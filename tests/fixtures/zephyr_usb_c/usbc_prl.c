/*
 * Copyright (c) 2022 The Chromium OS Authors
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Trimmed for the embarch-atlas tests from Zephyr v4.4.0 subsys/usb/usb_c/usbc_prl.c:
 * whole functions and whole state-table entries kept, everything else removed.
 */

/**
 * @brief Set the Protocol Layer Message Transmission state
 */
static void prl_tx_set_state(const struct device *dev, const enum usbc_prl_tx_state_t state)
{
	struct usbc_port_data *data = dev->data;
	struct protocol_layer_tx_t *prl_tx = data->prl_tx;

	__ASSERT(state < ARRAY_SIZE(prl_tx_states), "invalid prl_tx_state %d", state);
	smf_set_state(SMF_CTX(prl_tx), &prl_tx_states[state]);
}

/**
 * @brief Set the Protocol Layer Hard Reset state
 */
static void prl_hr_set_state(const struct device *dev, const enum usbc_prl_hr_state_t state)
{
	struct usbc_port_data *data = dev->data;
	struct protocol_hard_reset_t *prl_hr = data->prl_hr;

	__ASSERT(state < ARRAY_SIZE(prl_hr_states), "invalid prl_hr_state %d", state);
	smf_set_state(SMF_CTX(prl_hr), &prl_hr_states[state]);
}

#endif
#ifdef CONFIG_USBC_CSM_SUPPORTS_SINK
/**
 * @brief PRL_Tx_Snk_Start_of_AMS Entry State
 */
static void prl_tx_snk_start_ams_entry(void *obj)
{
	LOG_INF("PRL_Tx_Snk_Start_of_AMS");
}

/**
 * @brief PRL_Tx_Snk_Start_of_AMS Run State
 */
static enum smf_state_result prl_tx_snk_start_ams_run(void *obj)
{
	struct protocol_layer_tx_t *prl_tx = (struct protocol_layer_tx_t *)obj;
	const struct device *dev = prl_tx->dev;

	if (atomic_test_bit(&prl_tx->flags, PRL_FLAGS_MSG_XMIT)) {
		/*
		 * Don't clear pending XMIT flag here. Wait until we send so
		 * we can detect if we dropped this message or not.
		 */
		prl_tx_set_state(dev, PRL_TX_SNK_PENDING);
	}
	return SMF_EVENT_PROPAGATE;
}

#endif

#ifdef CONFIG_USBC_CSM_SUPPORTS_SINK
/**
 * @brief PRL_Tx_Snk_Pending Entry State
 */
static void prl_tx_snk_pending_entry(void *obj)
{
	LOG_INF("PRL_Tx_Snk_Pending");
}

/**
 * @brief PRL_Tx_Snk_Pending Run State
 */
static enum smf_state_result prl_tx_snk_pending_run(void *obj)
{
	struct protocol_layer_tx_t *prl_tx = (struct protocol_layer_tx_t *)obj;
	const struct device *dev = prl_tx->dev;
	struct usbc_port_data *data = dev->data;
	const struct device *tcpc = data->tcpc;
	enum tc_cc_voltage_state cc1;
	enum tc_cc_voltage_state cc2;

	/*
	 * Wait unit the SRC applies SINK_TX_OK so we can transmit.
	 */
	tcpc_get_cc(tcpc, &cc1, &cc2);

	/*
	 * We clear the pending XMIT flag here right before we send so
	 * we can detect if we discarded this message or not
	 */
	atomic_clear_bit(&prl_tx->flags, PRL_FLAGS_MSG_XMIT);

	/*
	 * The Protocol Layer Shall transition to the
	 * PRL_Tx_Layer_Reset_for_Transmit state when a Soft_Reset
	 * Message is pending.
	 */
	if ((prl_tx->msg_type == PD_CTRL_SOFT_RESET) && (prl_tx->emsg.len == 0)) {
		prl_tx_set_state(dev, PRL_TX_LAYER_RESET_FOR_TRANSMIT);
	} else if (cc1 == TC_CC_VOLT_RP_3A0 || cc2 == TC_CC_VOLT_RP_3A0) {
		/* If this is the first AMS message, inform the PE that it's been sent */
		if (atomic_test_bit(&prl_tx->flags, PRL_FLAGS_FIRST_MSG_PENDING)) {
			atomic_clear_bit(&prl_tx->flags, PRL_FLAGS_FIRST_MSG_PENDING);
			pe_first_msg_sent(dev);
		}

		/*
		 * The Protocol Layer Shall transition to the PRL_Tx_Construct_Message
		 * state when Rp is set to SinkTxOk and a Soft_Reset Message is not
		 * pending.
		 */

		/*
		 * Message pending (except Soft Reset) &
		 * Rp = SinkTxOk
		 */
		prl_tx_construct_message(dev);
		prl_tx_set_state(dev, PRL_TX_WAIT_FOR_PHY_RESPONSE);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief PRL_HR_Wait_for_Request Entry State
 *
 * @note This state is not part of the PRL_HR State Diagram found in
 *	 Figure 6-66. The PRL_HR state machine waits here until a
 *	 Hard Reset is requested by either the Policy Engine or the
 *	 PHY Layer.
 */
static void prl_hr_wait_for_request_entry(void *obj)
{
	struct protocol_hard_reset_t *prl_hr = (struct protocol_hard_reset_t *)obj;

	LOG_INF("PRL_HR_Wait_for_Request");

	/* Reset all Protocol Layer Hard Reset flags */
	prl_hr->flags = ATOMIC_INIT(0);
}

/**
 * @brief PRL_HR_Wait_for_Request Run State
 */
static enum smf_state_result prl_hr_wait_for_request_run(void *obj)
{
	struct protocol_hard_reset_t *prl_hr = (struct protocol_hard_reset_t *)obj;
	const struct device *dev = prl_hr->dev;

	/*
	 * The PRL_FLAGS_PE_HARD_RESET flag is set when a Hard Reset request is
	 * received from the Policy Engine.
	 *
	 * The PRL_FLAGS_PORT_PARTNER_HARD_RESET flag is set when Hard Reset
	 * signaling is received by the PHY Layer.
	 */
	if (atomic_test_bit(&prl_hr->flags, PRL_FLAGS_PE_HARD_RESET) ||
	    atomic_test_bit(&prl_hr->flags, PRL_FLAGS_PORT_PARTNER_HARD_RESET)) {
		/* Start Hard Reset */
		prl_hr_set_state(dev, PRL_HR_RESET_LAYER);
	}
	return SMF_EVENT_PROPAGATE;
}

/**
 * @brief PRL_HR_Reset_Layer Entry State
 */
static void prl_hr_reset_layer_entry(void *obj)
{
	struct protocol_hard_reset_t *prl_hr = (struct protocol_hard_reset_t *)obj;
	const struct device *dev = prl_hr->dev;
	struct usbc_port_data *data = dev->data;
	struct protocol_layer_rx_t *prl_rx = data->prl_rx;
	struct protocol_layer_tx_t *prl_tx = data->prl_tx;
	const struct device *tcpc = data->tcpc;
	int i;

	LOG_INF("PRL_HR_Reset_Layer");

	/* Reset all Protocol Layer message reception flags */
	prl_rx->flags = ATOMIC_INIT(0);
	/* Reset all Protocol Layer message transmission flags */
	prl_tx->flags = ATOMIC_INIT(0);

	/* Hard reset resets messageIDCounters for all TX types */
	for (i = 0; i < NUM_SOP_STAR_TYPES; i++) {
		prl_rx->msg_id[i] = -1;
		prl_tx->msg_id_counter[i] = 0;
	}

	/* Disable RX */
	tcpc_set_rx_enable(tcpc, false);

	/*
	 * PD r3.0 v2.0, ss6.2.1.1.5:
	 * After a physical or logical (USB Type-C Error Recovery) Attach, a
	 * Port discovers the common Specification Revision level between
	 * itself and its Port Partner and/or the Cable Plug(s), and uses this
	 * Specification Revision level until a Detach, Hard Reset or Error
	 * Recovery happens.
	 *
	 * This covers the Hard Reset case.
	 */
	prl_set_default_pd_revision(dev);

	/*
	 * Protocol Layer message transmission transitions to
	 * PRL_Tx_Wait_For_Message_Request state.
	 */
	prl_tx_set_state(dev, PRL_TX_PHY_LAYER_RESET);

	/*
	 * Protocol Layer message reception transitions to
	 * PRL_Rx_Wait_for_PHY_Message state.
	 *
	 * Note: The PRL_Rx_Wait_for_PHY_Message state is implemented
	 *	 as a single function, named prl_rx_wait_for_phy_message.
	 */

	/*
	 * Protocol Layer reset Complete &
	 * Hard Reset was initiated by Policy Engine
	 */
	if (atomic_test_bit(&prl_hr->flags, PRL_FLAGS_PE_HARD_RESET)) {
		/*
		 * Request PHY to perform a Hard Reset. Note
		 * PRL_HR_Request_Reset state is embedded here.
		 */
		prl_hr_send_msg_to_phy(dev);
		prl_hr_set_state(dev, PRL_HR_WAIT_FOR_PHY_HARD_RESET_COMPLETE);
	} else {
		/*
		 * Protocol Layer reset complete &
		 * Hard Reset was initiated by Port Partner
		 */

		/* Inform Policy Engine of the Hard Reset */
		pe_got_hard_reset(dev);
		prl_hr_set_state(dev, PRL_HR_WAIT_FOR_PE_HARD_RESET_COMPLETE);
	}
}

/* clang-format off */
/**
 * @brief Protocol Layer Transmit State table
 */
static const struct smf_state prl_tx_states[PRL_TX_STATE_COUNT] = {
	[PRL_TX_SNK_START_AMS] = SMF_CREATE_STATE(
		prl_tx_snk_start_ams_entry,
		prl_tx_snk_start_ams_run,
		NULL,
		NULL,
		NULL),
	[PRL_TX_SNK_PENDING] = SMF_CREATE_STATE(
		prl_tx_snk_pending_entry,
		prl_tx_snk_pending_run,
		NULL,
		NULL,
		NULL),
};

/**
 * @brief Protocol Layer Hard Reset State table
 */
static const struct smf_state prl_hr_states[PRL_HR_STATE_COUNT] = {
	[PRL_HR_WAIT_FOR_REQUEST] = SMF_CREATE_STATE(
		prl_hr_wait_for_request_entry,
		prl_hr_wait_for_request_run,
		NULL,
		NULL,
		NULL),
	[PRL_HR_RESET_LAYER] = SMF_CREATE_STATE(
		prl_hr_reset_layer_entry,
		NULL,
		NULL,
		NULL,
		NULL),
};
