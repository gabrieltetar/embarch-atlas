/*
 * Copyright (c) 2022 The Chromium OS Authors
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Trimmed for the embarch-atlas tests from Zephyr v4.4.0 subsys/usb/usb_c/usbc_pe_snk_states.c:
 * whole functions and whole state-table entries kept, everything else removed.
 */

/**
 * @brief PE_SNK_Startup Entry State
 */
void pe_snk_startup_entry(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;

	LOG_INF("PE_SNK_Startup");

	/* Reset the protocol layer */
	prl_reset(dev);

	/* Set power role to Sink */
	pe->power_role = TC_ROLE_SINK;

	/* Notify DPM of power role */
	policy_notify(dev, POWER_ROLE_IS_SINK);

	/* Invalidate explicit contract */
	atomic_clear_bit(pe->flags, PE_FLAGS_EXPLICIT_CONTRACT);

	/* Inform Device Policy Manager that PD is not connected */
	policy_notify(dev, NOT_PD_CONNECTED);
}

/**
 * @brief PE_SNK_Startup Run State
 */
enum smf_state_result pe_snk_startup_run(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;

	/*
	 * Once the reset process completes, the Policy Engine Shall
	 * transition to the PE_SNK_Discovery state
	 */
	if (prl_is_running(dev)) {
		pe_set_state(dev, PE_SNK_DISCOVERY);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief PE_SNK_Discovery Entry State
 */
void pe_snk_discovery_entry(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *vbus = data->vbus;

	LOG_INF("PE_SNK_Discovery");

	usbc_vbus_enable(vbus, true);
}

/**
 * @brief PE_SNK_Discovery Run State
 */
enum smf_state_result pe_snk_discovery_run(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *vbus = data->vbus;

	/*
	 * Transition to the PE_SNK_Wait_for_Capabilities state when
	 * VBUS has been detected
	 */
	if (usbc_vbus_check_level(vbus, TC_VBUS_PRESENT)) {
		pe_set_state(dev, PE_SNK_WAIT_FOR_CAPABILITIES);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief PE_SNK_Hard_Reset Entry State
 */
void pe_snk_hard_reset_entry(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;
	struct usbc_port_data *data = dev->data;

	LOG_INF("PE_SNK_Hard_Reset");

	/*
	 * Note: If the SinkWaitCapTimer times out and the HardResetCounter is
	 *       greater than nHardResetCount the Sink Shall assume that the
	 *       Source is non-responsive.
	 */
	if (atomic_test_bit(pe->flags, PE_FLAGS_SNK_WAIT_CAP_TIMEOUT) &&
	    pe->hard_reset_counter > PD_N_HARD_RESET_COUNT) {
		/* Inform the DPM that the port partner is not responsive */
		policy_notify(dev, PORT_PARTNER_NOT_RESPONSIVE);

		/* Pause the Policy Engine */
		data->pe_enabled = false;
		return;
	}

	/* Set Hard Reset Pending Flag */
	atomic_set_bit(pe->flags, PE_FLAGS_HARD_RESET_PENDING);

	atomic_clear_bit(pe->flags, PE_FLAGS_SNK_WAIT_CAP_TIMEOUT);

	/* Request the generation of Hard Reset Signaling by the PHY Layer */
	prl_execute_hard_reset(dev);
	/* Increment the HardResetCounter */
	pe->hard_reset_counter++;
}

/**
 * @brief PE_SNK_Hard_Reset Run State
 */
enum smf_state_result pe_snk_hard_reset_run(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;

	/*
	 * Transition to the PE_SNK_Transition_to_default state when:
	 *  1) The Hard Reset is complete.
	 */
	if (atomic_test_bit(pe->flags, PE_FLAGS_HARD_RESET_PENDING)) {
		return SMF_EVENT_PROPAGATE;
	}

	pe_set_state(dev, PE_SNK_TRANSITION_TO_DEFAULT);
	return SMF_EVENT_PROPAGATE;
}
