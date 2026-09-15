"""
查询改写（3.0 检索增强：解决词汇鸿沟）

原理（L04 讲义）：
    用户口语与知识库书面语无共同词时（如"学习成绩" vs "绩点/GPA"），
    任何匹配算法都对不上。解法：检索前把问题改写成知识库听得懂的话。
    注意：改写只影响【检索】，回答仍用原问题生成。

实现（方法 1：词典扩展）：
    内置一张「触发词 → 扩展词」映射表。查询命中触发词时，
    把扩展词追加到查询后面（原查询词保留，不是替换）。
    支持外部 JSON 词典（runtime/rewrite_dict.json），用户可自行增删；
    外部词典优先、内置词典兜底。

幂等性：
    若查询已包含触发词对应的全部扩展词，则不再追加——
    保证 run_eval.py --rewrite 传入改写查询时不会二次改写。
"""
import json
import os
from typing import Dict

import config_data as config


# ============================================================
# 内置默认词典（通用口语→术语映射）
# 注意：触发词不要互相包含（如"专业"与"专业排名"），否则会破坏幂等
#      领域专属词（如某专业的名称）请加到 runtime/rewrite_dict.json
# ============================================================
DEFAULT_DICT: Dict[str, list] = {
    "学习成绩": ["绩点", "GPA", "平均分", "专业排名", "奖学金"],
    "学习": ["绩点", "GPA", "课程成绩"],
    "证书": ["计算机二级", "普通话", "英语四级", "英语六级", "驾驶证"],
    "数据分析工具": ["Python", "Excel", "MySQL", "数据透视表", "VLOOKUP"],
    "工具": ["Python", "Excel", "MySQL"],
    "比赛": ["竞赛", "大赛"],
    "竞赛": ["比赛", "大赛"],
    "大学": ["学校", "学院", "专业"],
    "优势": ["数据分析", "统计建模", "数据预处理", "可视化"],
    "做了什么": ["工作内容", "项目内容", "成果"],
    "角色": ["数据分析员", "数据建模员", "PPT制作"],
}


# ============================================================
# 核心类：QueryRewriter
# ============================================================
class QueryRewriter:
    """
    词典法查询改写器

    用法：
        rw = QueryRewriter(enabled=True)
        rw.rewrite("他的学习成绩怎么样？")
        # → "他的学习成绩怎么样？ 绩点 GPA 平均分 专业排名 奖学金"
    """

    def __init__(self, dict_path: str = None, enabled: bool = True):
        self.enabled = enabled
        self.dict_path = dict_path or getattr(config, "REWRITE_DICT_PATH", None)
        self.term_map: Dict[str, list] = self._load_dict()

    def _load_dict(self) -> Dict[str, list]:
        """外部 JSON 词典优先，内置词典兜底，两层合并"""
        merged = {k: list(v) for k, v in DEFAULT_DICT.items()}
        if self.dict_path and os.path.exists(self.dict_path):
            try:
                with open(self.dict_path, encoding="utf-8") as f:
                    external = json.load(f)
                if isinstance(external, dict):
                    for k, v in external.items():
                        merged[k] = v if isinstance(v, list) else [str(v)]
            except Exception as e:
                print(f"[查询改写] 外部词典加载失败（{e}），使用内置词典")
        return merged

    def rewrite(self, query: str) -> str:
        """
        改写查询：命中触发词 → 追加扩展词（原查询保留）

        返回：
            改写后的查询字符串；无命中或已含全部扩展词时返回原查询（幂等）
        """
        if not self.enabled or not query:
            return query

        additions = []
        for trigger, words in self.term_map.items():
            if trigger in query:
                for w in words:
                    # 幂等：扩展词已存在于查询中就不再重复追加
                    if w and w not in query and w not in additions:
                        additions.append(w)

        if not additions:
            return query
        return f"{query} {(' ').join(additions)}"
