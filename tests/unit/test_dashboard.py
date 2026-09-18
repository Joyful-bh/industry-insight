from track_insight.reporting.dashboard import render_dashboard


def test_dashboard_is_self_contained_and_escapes_script_end() -> None:
    payload = {
        "plan": {
            "regions": ["北京市"],
            "industry_scopes": ["制造业"],
            "start_date": "2024-01-01",
            "end_date": "2026-01-01",
        },
        "metrics": {
            "track_count": 1,
            "candidate_count": 1,
            "watchlist_count": 0,
            "analyzed_count": 1,
            "topic_count": 2,
            "event_count": 3,
        },
        "tracks": [{"name": "机器视觉</script>", "status": "candidate"}],
    }

    html = render_dashboard(payload)

    assert "__DASHBOARD_DATA__" not in html
    assert "机器视觉<\\/script>" in html
    assert "热门赛道总览" in html
