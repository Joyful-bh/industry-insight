import pytest
from pydantic import ValidationError

from scripts.build_smb_dashboard import dashboard_payload, province_summary, render


def test_offline_dashboard_contains_latest_associations_and_safe_data():
    payload = dashboard_payload(
        {
            "tracks": [
                {
                    "name": "机床</script>",
                    "definition": "机床研发与制造",
                    "status": "candidate",
                    "identity_event_ids": ["E1", "E1"],
                    "topic_ids": ["T1"],
                    "smb_value_analysis": [
                        {
                            "workload": "设计仿真",
                            "product_category": "工作站",
                            "reason": "若进行工程仿真，可考虑工作站。",
                        }
                    ],
                }
            ]
        }
    )
    assert payload["tracks"][0]["event_count"] == 1
    assert payload["with_associations"] == 1
    html = render(payload)
    assert "__DATA__" not in html
    assert "机床\\u003c/script>" in html
    assert "工作站" in html and "已确认需求" in html
    assert "https://" not in html


def test_missing_association_output_is_not_presented_as_completed():
    with pytest.raises(ValidationError):
        dashboard_payload({"tracks": [{"name": "未分析"}]})


def test_empty_associations_are_valid():
    payload = dashboard_payload(
        {
            "tracks": [
                {
                    "name": "测试赛道",
                    "definition": "测试定义",
                    "status": "watchlist",
                    "smb_value_analysis": [],
                }
            ]
        }
    )
    assert payload["with_associations"] == 0
    assert payload["counts"] == {"watchlist": 1}


def test_all_support_events_are_deduplicated_and_sorted_not_just_core_events():
    document = {
        "tracks": [
            {
                "name": "较少证据",
                "definition": "定义一",
                "status": "candidate",
                "identity_event_ids": ["E1"],
                "smb_value_analysis": [],
            },
            {
                "name": "更多证据",
                "definition": "定义二",
                "status": "candidate",
                "identity_event_ids": ["E1"],
                "topic_ids": ["T1", "T1"],
                "supporting_topic_ids": ["T1", "T2"],
                "supporting_event_ids": ["E4"],
                "smb_value_analysis": [],
            },
        ]
    }
    evidence = {
        "topics": {
            "T1": {"event_ids": ["E1", "E2", "E2"]},
            "T2": {"event_ids": ["E2", "E3"]},
        },
        "events": {},
    }
    payload = dashboard_payload(document, evidence)
    first = payload["tracks"][0]
    assert first["name"] == "更多证据"
    assert first["event_count"] == 4 and first["topic_count"] == 2
    assert first["core_event_ids"] == ["E1"]
    assert set(first["event_ids"]) == {"E1", "E2", "E3", "E4"}
    html = render(payload)
    assert 'id="sort"' in html
    assert "topicHtml" in html and "eventHtml" in html
    assert 'rel="noopener noreferrer"' in html


def test_province_statistics_deduplicate_regions_without_guessing_city_or_nationwide():
    summary = province_summary(
        ["E1", "E1", "E2", "E3", "E4"],
        {
            "E1": {"regions": ["广东省", "广东省深圳市", "江苏省"]},
            "E2": {"regions": ["全国"]},
            "E3": {"regions": ["苏州市", "全球"]},
            "E4": {"regions": ["北京经济技术开发区", "北京市"]},
        },
    )
    assert {row["province"]: row["count"] for row in summary["province_counts"]} == {
        "广东省": 1,
        "江苏省": 1,
        "北京市": 1,
    }
    assert summary["localized_events"] == 2
    assert summary["national_events"] == 1
    assert summary["unmapped_events"] == 1


def test_region_values_are_included_in_dashboard():
    payload = dashboard_payload(
        {
            "tracks": [
                {
                    "name": "显示面板",
                    "definition": "面板制造",
                    "status": "candidate",
                    "identity_event_ids": ["E1"],
                    "smb_value_analysis": [],
                }
            ]
        },
        {"topics": {}, "events": {"E1": {"regions": ["安徽省"], "date": ""}}},
    )
    assert payload["tracks"][0]["province_counts"] == [{"province": "安徽省", "count": 1}]
    html = render(payload)
    assert '<dialog id="detail"' in html
    assert 'id="page-size"' in html and 'id="province"' in html
    assert 'data-tab="regions"' in html
