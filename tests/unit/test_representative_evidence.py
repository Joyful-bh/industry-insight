from datetime import date
from types import SimpleNamespace

from track_insight.tracks.preparation import select_representative_events


def event(i, title, objects, when=None):
    return SimpleNamespace(
        id=i, title=title, summary=title, industry_objects=objects, signal_date=when
    )


def test_product_project_precedes_recent_fund_and_platform():
    rows = [
        event(1, "设立电池材料产业基金", ["电池材料产业基金"], date(2026, 9, 1)),
        event(2, "建设成果转化平台", ["成果转化平台"], date(2026, 8, 1)),
        event(3, "布局固态电池及钠离子电池项目", ["固态电池", "钠离子电池"]),
    ]
    selected = select_representative_events(rows, {1: 1, 2: 2, 3: 3}, {})
    assert selected[0].id == 3
    assert len(selected) == 2


def test_same_page_does_not_hide_second_product_fact():
    rows = [
        event(1, "生产模具", ["模具"]),
        event(2, "年产显示芯片", ["显示芯片"]),
        event(3, "设立产业基金", ["产业基金"], date(2026, 9, 1)),
    ]
    assert {e.id for e in select_representative_events(rows, {1: 1, 2: 1, 3: 2}, {})} == {1, 2}


def test_inferred_object_absent_from_facts_gets_no_preference():
    rows = [event(1, "建设新能源基地", ["储能电池"]), event(2, "晶圆生产线投产", ["晶圆"])]
    assert select_representative_events(rows, {1: 1, 2: 2}, {})[0].id == 2


def test_empty_input_and_deterministic_order():
    assert select_representative_events([], {}, {}) == []
    rows = [event(i, "年产显示面板", ["显示面板"]) for i in (3, 1, 2)]
    assert [e.id for e in select_representative_events(rows, {1: 1, 2: 2, 3: 3}, {})] == [1, 2]
