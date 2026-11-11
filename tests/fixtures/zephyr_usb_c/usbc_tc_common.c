/*
 * Copyright (c) 2022 The Chromium OS Authors
 * Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Trimmed for the embarch-atlas tests from Zephyr v4.4.0 subsys/usb/usb_c/usbc_tc_common.c:
 * whole functions and whole state-table entries kept, everything else removed.
 */

/**
 * @brief Sets a Type-C state
 */
void tc_set_state(const struct device *dev, const enum tc_state_t state)
{
	struct usbc_port_data *data = dev->data;
	struct tc_sm_t *tc = data->tc;

	__ASSERT(state < ARRAY_SIZE(tc_states), "invalid tc_state %d", state);
	smf_set_state(SMF_CTX(tc), &tc_states[state]);
}

/**
 * @brief Type-C State Table
 */
/* clang-format off */
static const struct smf_state tc_states[TC_STATE_COUNT] = {
	[TC_CC_RD_SUPER_STATE] = SMF_CREATE_STATE(
		tc_cc_rd_entry,
		NULL,
		NULL,
		NULL,
		NULL),
	[TC_UNATTACHED_SNK_STATE] = SMF_CREATE_STATE(
		tc_unattached_snk_entry,
		tc_unattached_snk_run,
		NULL,
		&tc_states[TC_CC_RD_SUPER_STATE],
		NULL),
	[TC_ATTACH_WAIT_SNK_STATE] = SMF_CREATE_STATE(
		tc_attach_wait_snk_entry,
		tc_attach_wait_snk_run,
		tc_attach_wait_snk_exit,
		&tc_states[TC_CC_RD_SUPER_STATE],
		NULL),
	[TC_ATTACHED_SNK_STATE] = SMF_CREATE_STATE(
		tc_attached_snk_entry,
		tc_attached_snk_run,
		tc_attached_snk_exit,
		NULL,
		NULL),
};
