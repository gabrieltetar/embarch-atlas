/*
 * Copyright (c) 2022 The Chromium OS Authors
 * Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Trimmed for the embarch-atlas tests from Zephyr v4.4.0 subsys/usb/usb_c/usbc_pe_common.c:
 * whole functions and whole state-table entries kept, everything else removed.
 */

/**
 * @brief Sets a Policy Engine state
 */
void pe_set_state(const struct device *dev, const enum usbc_pe_state state)
{
	struct usbc_port_data *data = dev->data;

	__ASSERT(state < ARRAY_SIZE(pe_states), "invalid pe_state %d", state);
	smf_set_state(SMF_CTX(data->pe), &pe_states[state]);

	/* Allow the PE to execute logic from the new state without additional delay */
	usbc_bypass_next_sleep(dev);
}

/**
 * @brief Send a soft reset message
 */
void pe_send_soft_reset(const struct device *dev, const enum pd_packet_type type)
{
	struct usbc_port_data *data = dev->data;

	data->pe->soft_reset_sop = type;
	pe_set_state(dev, PE_SEND_SOFT_RESET);
}

/**
 * @brief PE_Send_Soft_Reset Entry State
 *	  NOTE: Sender Response Timer is handled in super state.
 */
static void pe_send_soft_reset_entry(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;

	LOG_INF("PE_SNK_Send_Soft_Reset");

	/* Reset Protocol Layer */
	prl_reset(dev);
	atomic_set_bit(pe->flags, PE_FLAGS_SEND_SOFT_RESET);
}

/**
 * @brief PE_Send_Soft_Reset Run State
 */
static enum smf_state_result pe_send_soft_reset_run(void *obj)
{
	struct policy_engine *pe = (struct policy_engine *)obj;
	const struct device *dev = pe->dev;
	struct usbc_port_data *data = dev->data;
	struct protocol_layer_rx_t *prl_rx = data->prl_rx;
	union pd_header header;

	if (prl_is_running(dev) == false) {
		return SMF_EVENT_PROPAGATE;
	}

	if (atomic_test_and_clear_bit(pe->flags, PE_FLAGS_SEND_SOFT_RESET)) {
		/* Send Soft Reset message */
		pe_send_ctrl_msg(dev, pe->soft_reset_sop, PD_CTRL_SOFT_RESET);
		return SMF_EVENT_PROPAGATE;
	}

	if (atomic_test_and_clear_bit(pe->flags, PE_FLAGS_MSG_DISCARDED)) {
		/* Inform Device Policy Manager that the message was discarded */
		policy_notify(dev, MSG_DISCARDED);
		pe_set_ready_state(dev);
	} else if (atomic_test_and_clear_bit(pe->flags, PE_FLAGS_MSG_RECEIVED)) {
		/*
		 * The Policy Engine Shall transition to the PE_SNK_Wait_for_Capabilities
		 * state when:
		 *      1: An Accept Message has been received on SOP
		 */
		header = prl_rx->emsg.header;

		if (received_control_message(dev, header, PD_CTRL_ACCEPT)) {
			pe_set_state(dev, PE_SNK_WAIT_FOR_CAPABILITIES);
		}
	} else if (atomic_test_and_clear_bit(pe->flags, PE_FLAGS_PROTOCOL_ERROR)) {
		/*
		 * The Policy Engine Shall transition to the PE_SNK_Hard_Reset state when:
		 *      1: A SenderResponseTimer timeout occurs (Handled in Super State)
		 *      2: Or the Protocol Layer indicates that a transmission error has occurred
		 */
		pe_set_state(dev, PE_SNK_HARD_RESET);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief Policy engine State table
 */
/* clang-format off */
static const struct smf_state pe_states[PE_STATE_COUNT] = {
	[PE_SNK_STARTUP] = SMF_CREATE_STATE(
		pe_snk_startup_entry,
		pe_snk_startup_run,
		NULL,
		NULL,
		NULL),
	[PE_SNK_DISCOVERY] = SMF_CREATE_STATE(
		pe_snk_discovery_entry,
		pe_snk_discovery_run,
		NULL,
		NULL,
		NULL),
	[PE_SNK_HARD_RESET] = SMF_CREATE_STATE(
		pe_snk_hard_reset_entry,
		pe_snk_hard_reset_run,
		NULL,
		NULL,
		NULL),
#ifdef CONFIG_USBC_CSM_SUPPORTS_SOURCE
	[PE_SRC_STARTUP] = SMF_CREATE_STATE(
		pe_src_startup_entry,
		pe_src_startup_run,
		NULL,
		NULL,
		NULL),
#endif
	[PE_SEND_SOFT_RESET] = SMF_CREATE_STATE(
		pe_send_soft_reset_entry,
		pe_send_soft_reset_run,
		NULL,
		&pe_states[PE_SENDER_RESPONSE_PARENT],
		NULL),
};
