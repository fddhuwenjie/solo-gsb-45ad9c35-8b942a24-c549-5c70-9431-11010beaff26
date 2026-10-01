"""启动时初始化示例禁配规则（仅当规则表为空时写入）。"""
from sqlalchemy.orm import Session

from .models import SegregationRule

# 参考《危险货物道路运输规则》常见隔离要求的示例数据
SAMPLE_RULES = [
    ("2.1", "5.1", "forbidden", "易燃气体与氧化剂禁止同舱"),
    ("3", "5.1", "forbidden", "易燃液体与氧化剂禁止同舱"),
    ("4.1", "5.1", "forbidden", "易燃固体与氧化剂禁止同舱"),
    ("4.2", "5.1", "forbidden", "自燃物质与氧化剂禁止同舱"),
    ("4.3", "5.1", "forbidden", "遇水放出易燃气体物质与氧化剂禁止同舱"),
    ("3", "6.1", "forbidden", "易燃液体与毒性物质禁止同舱"),
    ("6.1", "8", "forbidden", "毒性物质与腐蚀性物质禁止同舱"),
]


def seed_rules(db: Session) -> int:
    """规则表为空时写入示例规则，返回写入条数。"""
    if db.query(SegregationRule).count() > 0:
        return 0
    for class_a, class_b, level, desc in SAMPLE_RULES:
        db.add(SegregationRule(class_a=class_a, class_b=class_b, level=level, description=desc))
    db.commit()
    return len(SAMPLE_RULES)
