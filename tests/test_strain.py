"""`derive strain-budget`: every formula against a hand calculation, to three significant figures."""
import math

import pytest

from embarch_atlas import cli, strain

E_STEEL = "200 GPa"


def spec(section, load, **ro):
    r = {"gauge_factor": 2.0, "bridge": "full", "excitation": "5 V", "adc_noise_density": "50 nV/sqrt(Hz)", "bandwidth": "10 Hz"}
    if "adc_noise_rms" in ro:
        r.pop("adc_noise_density")
        r.pop("bandwidth")
    r.update(ro)
    return {"section": section, "material": {"youngs_modulus": E_STEEL, "shear_modulus": "80 GPa"}, "load": load, "readout": r}


def rows(s):
    _, rs = strain.budget(s)
    return {n: v for n, v, _, _ in rs}


def test_solid_rod_hand_case():
    # d=10 mm: I=pi*1e-8/64=4.909e-10, S=pi*1e-6/32=9.817e-8, A=7.854e-5, J=9.817e-10
    r = rows(spec({"type": "solid_round", "outer_diameter": "10 mm"},
                  {"force": "100 N", "lever_arm": "0.1 m", "axial_force": "1000 N", "torque": "5 N*m"}))
    assert r["second moment of area I"] == pytest.approx(4.909e-10, rel=1e-3)
    assert r["section modulus S (outer fibre)"] == pytest.approx(9.817e-8, rel=1e-3)
    assert r["bending strain, outer surface"] == pytest.approx(5.093e-4, rel=1e-3)   # 10/(200e9*9.817e-8)
    assert r["tip deflection"] == pytest.approx(3.395e-4, rel=1e-3)                    # 100*1e-3/(3*200e9*4.909e-10)
    assert r["axial strain"] == pytest.approx(6.366e-5, rel=1e-3)                      # 1000/(200e9*7.854e-5)
    assert r["torsional shear strain, outer surface"] == pytest.approx(3.183e-4, rel=1e-3)  # 5*0.005/(9.817e-10*80e9)
    assert r["torsional principal strain (gauge at 45 deg)"] == pytest.approx(1.592e-4, rel=1e-3)


def test_thin_wall_tube_hand_case_and_inner_wall():
    # D=20, t=1 mm: d_i=18; I=pi*(1.6e-7-1.04976e-7)/64=2.701e-9; M=10 N*m
    r = rows(spec({"type": "round_tube", "outer_diameter": "20 mm", "wall_thickness": "1 mm"}, {"moment": "10 N*m", "torque": "4 N*m"}))
    assert r["second moment of area I"] == pytest.approx(2.701e-9, rel=1e-3)
    assert r["bending strain, outer surface"] == pytest.approx(1.851e-4, rel=1e-3)   # 10*0.010/(200e9*2.701e-9)
    assert r["bending strain, inner wall"] == pytest.approx(1.666e-4, rel=1e-3)      # 10*0.009/(200e9*2.701e-9)
    assert r["bending strain, inner wall"] / r["bending strain, outer surface"] == pytest.approx(18 / 20)
    # thin-wall approximation I ~ pi*r_mean^3*t, r_mean=9.5 mm, agrees within 1%
    assert r["second moment of area I"] == pytest.approx(math.pi * 9.5e-3 ** 3 * 1e-3, rel=0.01)
    # J=2I=5.402e-9; gamma_out=4*0.010/(5.402e-9*80e9)=9.256e-5; inner is 0.9 of it
    assert r["torsional shear strain, outer surface"] == pytest.approx(9.256e-5, rel=1e-3)
    assert r["torsional shear strain, inner wall"] == pytest.approx(8.330e-5, rel=1e-3)
    assert "tip deflection" not in r


def test_rectangular_flexure_hand_case():
    # b=10, h=1 mm, L=20 mm, F=10 N, E=70 GPa: I=8.333e-13, S=1.667e-9, M=0.2
    s = spec({"type": "rectangular", "width": "10 mm", "thickness": "1 mm"}, {"force": "10 N", "lever_arm": "20 mm"})
    s["material"]["youngs_modulus"] = "70 GPa"
    r = rows(s)
    assert r["second moment of area I"] == pytest.approx(8.333e-13, rel=1e-3)
    assert r["section modulus S (outer fibre)"] == pytest.approx(1.667e-9, rel=1e-3)
    assert r["bending strain, outer surface"] == pytest.approx(1.714e-3, rel=1e-3)   # 0.2/(70e9*1.667e-9)
    assert r["tip deflection"] == pytest.approx(4.571e-4, rel=1e-3)                  # 10*8e-6/(3*70e9*8.333e-13)


def test_bridge_output_and_noise_limit():
    sec = {"type": "solid_round", "outer_diameter": "10 mm"}
    ld = {"moment": "10 N*m"}
    for bridge, per_ue in (("quarter", 2.5e-6), ("half", 5e-6), ("full", 10e-6)):   # Vex*k*GF*1e-6, GF=2, Vex=5
        r = rows(spec(sec, ld, bridge=bridge))
        assert r["bridge output per microstrain"] == pytest.approx(per_ue, rel=1e-3)
        # v_n = 50e-9*sqrt(10)=1.581e-7 V; eps_res = v_n/(Vex*k*GF)
        assert r["noise-limited resolvable strain (1 sigma)"] == pytest.approx(1.581e-7 / (per_ue * 1e6), rel=1e-3)
    r = rows(spec(sec, ld))                       # full: Vout = 10e-6 V/ue * 509.3 ue = 5.093e-3 V
    assert r["bridge output, bending strain"] == pytest.approx(5.093e-3, rel=1e-3)
    assert r["signal-to-noise ratio, bending"] == pytest.approx(5.093e-3 / 1.581e-7, rel=1e-3)
    assert r["noise-limited resolvable bending moment"] == pytest.approx(1.581e-8 * 200e9 * 9.817e-8, rel=2e-3)


def test_rms_noise_alternative():
    r = rows(spec({"type": "solid_round", "outer_diameter": "10 mm"}, {"moment": "1 N*m"}, adc_noise_rms="2 uV", data_rate="10 Hz"))
    assert r["noise-limited resolvable strain (1 sigma)"] == pytest.approx(2e-6 / (5 * 2), rel=1e-6)


@pytest.mark.parametrize("mutate,words", [
    (lambda s: s["load"].update(force="100"), "load.force: no unit"),
    (lambda s: s["section"].update(type="i_beam"), "section.type"),
    (lambda s: s["section"].update(outer_diameter="10 N"), "not a length unit"),
    (lambda s: s["load"].update(torque="1 N*m") or s["section"].update(type="rectangular", width="1 mm", thickness="1 mm"), "rectangular"),
    (lambda s: s["readout"].pop("bandwidth"), "readout.bandwidth: missing"),
])
def test_refusals_are_one_line_naming_the_input(mutate, words):
    s = spec({"type": "solid_round", "outer_diameter": "10 mm"}, {"moment": "1 N*m"})
    mutate(s)
    with pytest.raises(ValueError, match=words) as e:
        strain.budget(s)
    assert "\n" not in str(e.value)


def test_cli_prints_derived_rows_with_formulas(tmp_path, capsys):
    f = tmp_path / "b.toml"
    f.write_text('''[section]
type = "round_tube"
outer_diameter = "20 mm"
wall_thickness = "1 mm"
[material]
youngs_modulus = "200 GPa"
[load]
force = "100 N"
lever_arm = "0.1 m"
[readout]
gauge_factor = 2.0
bridge = "half"
excitation = "5 V"
adc_noise_rms = "1 uV"
''')
    assert cli.main(["derive", "strain-budget", str(f)]) == 0
    out = capsys.readouterr().out
    body = [ln for ln in out.splitlines() if ln.startswith("| ") and "---" not in ln][1:]
    assert body and all("(derived)" in ln and ln.count("|") == 5 and ln.split("|")[4].strip() for ln in body)
    f.write_text("[section]\n")
    assert cli.main(["derive", "strain-budget", str(f)]) == 2
    assert "material: missing table" in capsys.readouterr().err
