from app.api.llm_keys import _anthropic_slot_map


def test_anthropic_slot_map_uses_canonical_slot_not_priority_order():
    records = [
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN_3",
            "slot": "3",
            "priority": 1,
        },
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN_2",
            "slot": "2",
            "priority": 2,
        },
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN",
            "slot": "1",
            "priority": 3,
        },
    ]

    assert _anthropic_slot_map(records) == {
        "ANTHROPIC_AUTH_TOKEN_3": "slot3",
        "ANTHROPIC_AUTH_TOKEN_2": "slot2",
        "ANTHROPIC_AUTH_TOKEN": "slot1",
    }


def test_anthropic_slot_map_ignores_unaddressable_records():
    assert _anthropic_slot_map(
        [
            {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "slot": ""},
            {"key_name": "", "slot": "4"},
        ]
    ) == {}
