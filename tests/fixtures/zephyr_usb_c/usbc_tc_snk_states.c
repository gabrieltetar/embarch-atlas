/*
 * Copyright (c) 2022 The Chromium OS Authors
 * Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Trimmed for the embarch-atlas tests from Zephyr v4.4.0 subsys/usb/usb_c/usbc_tc_snk_states.c:
 * whole functions and whole state-table entries kept, everything else removed.
 */

/**
 * @brief Unattached.SNK Entry
 */
void tc_unattached_snk_entry(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;

	LOG_INF("Unattached.SNK");

#ifdef CONFIG_USBC_CSM_DRP
	/* Start DRP toggle timer with tDRP Sink time */
	usbc_timer_start_with_value(&tc->tc_t_drp_toggle, TC_T_DRP_SNK_MS);
#endif

	/*
	 * Allow the state machine to immediately check the state of CC lines and go into
	 * Attach.Wait state in case the Rp value is detected on the CC lines
	 */
	usbc_bypass_next_sleep(tc->dev);
}

/**
 * @brief Unattached.SNK Run
 */
enum smf_state_result tc_unattached_snk_run(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *vbus = data->vbus;

	/*
	 * Transition to AttachWait.SNK when the SNK.Rp state is present
	 * on at least one of its CC pins.
	 */
	if (tcpc_is_cc_rp(tc->cc1) || tcpc_is_cc_rp(tc->cc2)) {
#ifdef CONFIG_USBC_CSM_DRP
		/* Stop DRP toggle timer when CC connection detected */
		usbc_timer_stop(&tc->tc_t_drp_toggle);
#endif
		usbc_vbus_enable(vbus, true);
		tc_set_state(dev, TC_ATTACH_WAIT_SNK_STATE);
		return SMF_EVENT_HANDLED;
	}

#ifdef CONFIG_USBC_CSM_DRP
	/* Check if DRP toggle timer expired - transition to Unattached.SRC */
	if (usbc_timer_expired(&tc->tc_t_drp_toggle)) {
		tc_set_state(dev, TC_UNATTACHED_SRC_STATE);
		/* Execute transition immediately to improve DRP timing accuracy */
		usbc_bypass_next_sleep(tc->dev);
		return SMF_EVENT_HANDLED;
	}
#endif

	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief AttachWait.SNK Entry
 */
void tc_attach_wait_snk_entry(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;

	LOG_INF("AttachWait.SNK");

	tc->cc_state = TC_CC_NONE;

	/*
	 * Allow the debounce timers to start immediately without additional delay added
	 * by going into sleep
	 */
	usbc_bypass_next_sleep(tc->dev);
}

/**
 * @brief AttachWait.SNK Run
 */
enum smf_state_result tc_attach_wait_snk_run(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *vbus = data->vbus;
	enum tc_cc_states new_cc_state;
	bool vbus_present;

	if (tcpc_is_cc_rp(tc->cc1) || tcpc_is_cc_rp(tc->cc2)) {
		new_cc_state = TC_CC_DFP_ATTACHED;
	} else {
		new_cc_state = TC_CC_NONE;
	}

	/* Debounce the cc state */
	if (new_cc_state != tc->cc_state) {
		usbc_timer_start(&tc->tc_t_cc_debounce);
		tc->cc_state = new_cc_state;
	}

	/* Wait for CC debounce */
	if (usbc_timer_running(&tc->tc_t_cc_debounce) &&
	    usbc_timer_expired(&tc->tc_t_cc_debounce) == false) {
		if (CONFIG_USBC_STATE_MACHINE_CYCLE_TIME >= TC_T_CC_DEBOUNCE_MIN_MS) {
			/* Make sure the debounce time won't be longer than specified */
			usbc_bypass_next_sleep(tc->dev);
		}

		return SMF_EVENT_PROPAGATE;
	}

	/* Transition to UnAttached.SNK if CC lines are open */
	if (new_cc_state == TC_CC_NONE) {
		tc_set_state(dev, TC_UNATTACHED_SNK_STATE);
	}

	/*
	 * The port shall transition to Attached.SNK after the state of only
	 * one of the CC1 or CC2 pins is SNK.Rp for at least tCCDebounce and
	 * VBUS is detected.
	 */
	vbus_present = usbc_vbus_check_level(vbus, TC_VBUS_PRESENT);

	if (vbus_present) {
		tc_set_state(dev, TC_ATTACHED_SNK_STATE);
	}

	/*
	 * In case of no VBUS present, this call prevents going into the sleep and allows for
	 * faster VBUS detection. In case of VBUS present, allows for immediate execution of logic
	 * from new state.
	 */
	usbc_bypass_next_sleep(tc->dev);
	return SMF_EVENT_PROPAGATE;
}

void tc_attach_wait_snk_exit(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;

	usbc_timer_stop(&tc->tc_t_cc_debounce);
}

/**
 * @brief Attached.SNK Entry
 */
void tc_attached_snk_entry(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *tcpc = data->tcpc;
	int ret;

	LOG_INF("Attached.SNK");

	/* Clear cached CC voltage */
	tc->cc_voltage = TC_CC_VOLT_OPEN;

	/* Set CC polarity */
	ret = tcpc_set_cc_polarity(tcpc, tc->cc_polarity);
	if (ret != 0) {
		LOG_ERR("Couldn't set CC polarity to %d: %d", tc->cc_polarity, ret);
		tc_set_state(dev, TC_ERROR_RECOVERY_STATE);
		return;
	}

	/* Enable PD */
	tc_pd_enable(dev, true);

	/* Enable sink path for the PPC */
	if (data->ppc != NULL) {
		ret = ppc_set_snk_ctrl(data->ppc, true);
		if (ret != 0 && ret != -ENOTSUP) {
			LOG_ERR("Couldn't enable PPC sink path: %d", ret);
		}
	}
}

/**
 * @brief Attached.SNK and DebugAccessory.SNK Run
 */
enum smf_state_result tc_attached_snk_run(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *vbus = data->vbus;

	/* Detach detection */
	if (usbc_vbus_check_level(vbus, TC_VBUS_REMOVED)) {
		usbc_vbus_enable(vbus, false);
		tc_set_state(dev, TC_UNATTACHED_SNK_STATE);
		return SMF_EVENT_PROPAGATE;
	}

	/* Run Sink Power Sub-State if not in an explicit contract */
	if (pe_is_explicit_contract(dev) == false) {
		sink_power_sub_states(dev);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief Attached.SNK and DebugAccessory.SNK Exit
 */
void tc_attached_snk_exit(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	int ret;

	/* Disable PD */
	tc_pd_enable(dev, false);

	/* Inform Device Policy Manager of the sink power change */
	policy_notify(dev, POWER_CHANGE_0A0);

	/* Disable sink path for the PPC */
	if (data->ppc != NULL) {
		ret = ppc_set_snk_ctrl(data->ppc, false);
		if (ret != 0 && ret != -ENOTSUP) {
			LOG_ERR("Couldn't disable PPC sink path: %d", ret);
		}
	}
}

/**
 * @brief Rd on CC lines Entry
 */
void tc_cc_rd_entry(void *obj)
{
	struct tc_sm_t *tc = (struct tc_sm_t *)obj;
	const struct device *dev = tc->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *tcpc = data->tcpc;
	int ret;

	ret = tcpc_set_cc(tcpc, TC_CC_RD);
	if (ret != 0) {
		LOG_ERR("Couldn't set CC lines to Rd: %d", ret);
		tc_set_state(dev, TC_ERROR_RECOVERY_STATE);
	}
}
