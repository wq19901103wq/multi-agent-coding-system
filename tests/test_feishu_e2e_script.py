import pytest

from scripts.feishu_e2e import (
    _confirmation_path_for_rework,
    _extract_message_text,
    _is_bind_dir_interaction,
    _structured_recovery_blocker,
)


def test_extract_message_text_reads_hidden_approval_callback_metadata():
    card = {
        "header": {"title": {"content": "确认需求"}},
        "elements": [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"content": "确认"},
                        "value": {
                            "approval_id": "appr_structured_1",
                            "action": "confirm",
                        },
                    }
                ],
            }
        ],
    }

    searchable = _extract_message_text(card)

    assert "确认需求" in searchable
    assert "审批ID：appr_structured_1" in searchable


def test_extract_message_text_reads_stable_menu_action_metadata():
    card = {
        "header": {"title": {"content": "为 feature 项目选择目录"}},
        "elements": [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"content": "使用系统工作区"},
                        "value": {
                            "action": "menu",
                            "option_key": "bind_dir_for_create_skip",
                        },
                    }
                ],
            }
        ],
    }

    searchable = _extract_message_text(card)

    assert "动作：bind_dir_for_create_skip" in searchable


@pytest.mark.parametrize(
    "text",
    [
        "动作：bind_dir_for_create_skip",
        "为 feature 项目选择目录",
        "选择一个目录绑定后创建项目，或使用系统工作区",
    ],
)
def test_bind_dir_interaction_survives_card_copy_and_schema_versions(text):
    assert _is_bind_dir_interaction(text)


def test_rework_path_honors_any_structured_upstream_target():
    assert _confirmation_path_for_rework(
        "to_specs", "REVIEW_CONFIRM_PENDING"
    ) == [
        "IMPLEMENTATION_SPEC_CONFIRM_PENDING",
        "CODING_CONFIRM_PENDING",
        "TESTING_CONFIRM_PENDING",
    ]


def test_rework_path_rejects_downstream_target():
    with pytest.raises(RuntimeError, match="downstream"):
        _confirmation_path_for_rework(
            "to_testing", "CODING_CONFIRM_PENDING"
        )


def test_non_retryable_recovery_is_not_auto_retried():
    blocker = _structured_recovery_blocker({
        "recovery": {
            "domain": "upstream_api",
            "status_code": 402,
            "retryable": False,
        }
    })

    assert blocker is not None
    assert "status=402" in blocker
    assert _structured_recovery_blocker({
        "recovery": {
            "domain": "upstream_api",
            "status_code": 503,
            "retryable": True,
        }
    }) is None
