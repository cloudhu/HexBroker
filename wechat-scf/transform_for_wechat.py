#!/usr/bin/env python3
"""
云侠公众号内容润色脚本 v1.5

将自动化任务生成的交易报告改写为通俗易懂的公众号文章。
润色规则：
1. 版权声明：AI自动生成 + 模拟盘 + 不构成投资建议
2. 个股代码中间插入统一混淆符号 +（仅对本地数据库中的真实A股代码）
3. 公司名称中间用 + 替换一个字（仅对本地数据库或已知列表中的真实公司名）
4. 英文策略名称替换成中文
5. 英文术语/参数名/表头翻译成中文（v1.1新增）
6. 本地沪深A股数据库精确查询（v1.3新增）：基于5198+只沪深A股数据库匹配，
   彻底避免普通文字被误判为公司名/股票代码
7. --no-obfuscate 跳过股票混淆（v1.4新增）：故事类文章保留真实股票名
8. 修复中文强调语法失效（v1.5新增）：**「xxx」** 紧贴中文标点时不符合
   CommonMark flanking 规则，星号会原样漏进公众号正文，故统一转内联 HTML

Usage:
    python transform_for_wechat.py <input.md> <output.md> [--title TITLE] [--cover COVER] [--no-obfuscate]
"""

import re
import random
import argparse
import os
import sys
import json
from datetime import datetime
from pathlib import Path


# ============================================================
# 规则 1：版权声明模板
# ============================================================
COPYRIGHT_TEMPLATE = """

---

> **版权声明**：本文由「云侠」AI量化交易系统自动生成。云侠持仓的账户为**模拟盘**，并非实盘，文中观点**不构成任何投资建议**。投资有风险，入市需谨慎。

"""

COPYRIGHT_MD_PREFIX = """

> ⚠️ **本文由「云侠」AI量化交易系统自动生成，持仓账户为模拟盘而非实盘，文中观点不构成任何投资建议。投资有风险，入市需谨慎。**

"""


# ============================================================
# 规则 2：个股代码和公司名称混淆
# ============================================================

def obfuscate_stock_code(code: str) -> str:
    """个股代码中间插入统一混淆符号 +。

    6位代码 → 在第3位和第4位之间插入 1 个 +
    例：600027 → 600+027

    注：统一使用 + 作为混淆符号（2026-07-22 由 ~ 改）。+ 仅作数字间的纯文本分隔，
    不触发 Markdown 标题(#)/粗体斜体(*)/实体(&)/删除线(~~) 等语法；且混淆结果永远内联
    （600+027），不会出现在行首，故也不构成 Markdown 列表标记（+ item）。
    """
    if not code or len(code) < 4:
        return code
    
    # 确认是纯数字的股票代码（6位A股代码或5位港股代码）
    if not code.isdigit():
        return code
    
    # 在代码中间位置插入统一混淆符号 +
    mid = len(code) // 2
    return code[:mid] + "+" + code[mid:]


def obfuscate_company_name(name: str) -> str:
    """公司名称中间用一个 + 替换一字（统一混淆符号，避免与 Markdown 特殊字符混杂）。
    
    例：华电国际 → 华电+际
    例：紫金矿业 → 紫金+业 或 紫+矿业
    
    规则：
    - 2字名称：第1字替换
    - 3字名称：第2字替换
    - 4字及以上：随机选中间位置（不在首尾）替换
    - 统一符号：+（2026-07-22 由 ~ 改；+ 内联于汉字间，不触发 Markdown 列表/标题/粗体等语法）
    """
    if not name or len(name) < 2:
        return name
    
    # 单字不替换
    if len(name) == 1:
        return name
    
    # 统一混淆符号
    replace_symbol = "+"
    
    if len(name) == 2:
        # 2字名称：替换第1字
        return replace_symbol + name[1]
    elif len(name) == 3:
        # 3字名称：替换中间字
        return name[0] + replace_symbol + name[2]
    else:
        # 4字及以上：随机选中间位置（排除首尾）
        candidates = list(range(1, len(name) - 1))
        pos = random.choice(candidates)
        return name[:pos] + replace_symbol + name[pos+1:]


# ------------------------------------------------------------
# v1.3 新增：本地沪深A股数据库加载
# ------------------------------------------------------------

def load_local_stock_database() -> tuple[dict, dict]:
    """加载本地沪深A股数据库，返回 (code_to_name, name_to_code)。
    
    数据库路径：脚本同级目录的 ../data/a_stock_database.json
    若文件不存在，则返回空字典，并打印警告。
    """
    script_dir = Path(__file__).parent.resolve()
    db_path = script_dir.parent / 'data' / 'a_stock_database.json'
    
    code_to_name = {}
    name_to_code = {}
    
    if db_path.exists():
        try:
            with open(db_path, 'r', encoding='utf-8') as f:
                db = json.load(f)
            code_to_name = db.get('code_to_name', {})
            name_to_code = db.get('name_to_code', {})
            print(f"   - 已加载本地沪深A股数据库: {len(code_to_name)} 只")
        except Exception as e:
            print(f"   ⚠️ 加载本地数据库失败: {e}")
    else:
        print(f"   ⚠️ 本地数据库不存在: {db_path}")
        print(f"      请运行: python update_stock_database.py")
    
    return code_to_name, name_to_code


LOCAL_CODE_TO_NAME, LOCAL_NAME_TO_CODE = load_local_stock_database()

# ------------------------------------------------------------
# 补充公司名称-代码对照表（v1.3 角色：数据库未覆盖标的的补充）
# ------------------------------------------------------------
# 注意：v1.3 主要检测来源已改为本地沪深A股数据库（5198+只）。
# 此表仅用于补充：港股、北交所、基金、指数、简称等数据库未覆盖标的。
# 新增补充项前请确认不会导致普通文字误判。
KNOWN_STOCKS = {
    # 原有31只
    "601899": "紫金矿业", "600104": "宇通客车", "601336": "新华保险",
    "002458": "益生股份", "600027": "华电国际", "000725": "京东方A",
    "002049": "紫光国微", "002371": "北方华创", "300750": "宁德时代",
    "601012": "隆基绿能", "000858": "五粮液", "600519": "贵州茅台",
    "000001": "平安银行", "600036": "招商银行", "601166": "兴业银行",
    "002230": "科大讯飞", "300059": "东方财富", "002415": "海康威视",
    "000002": "万科A", "601688": "华泰证券", "600900": "长江电力",
    "000568": "泸天化", "600298": "安琪酵母", "002709": "天赐材料",
    "300124": "汇川技术", "603259": "药明康德", "600585": "海螺水泥",
    "002714": "牧原股份", "000661": "长春高新", "600809": "山西汾酒",
    # v1.1 新增 — 自选股池
    "600030": "中信证券", "000975": "山金国际", "600547": "山东黄金",
    "600489": "中金黄金", "600988": "赤峰黄金", "600150": "中国船舶",
    "601689": "拓普集团", "300308": "中际旭创", "002407": "多氟多",
    # v1.1 新增 — 报告中常见标的
    "002326": "永太科技", "688012": "中微公司", "688041": "海光信息",
    "688498": "源杰科技", "300476": "胜宏科技", "300604": "长川科技",
    "002407": "多氟多", "600066": "宇通客车",
    # v1.1 新增 — 条件选股/标签选股常见
    "002475": "立讯精密", "002594": "比亚迪", "688017": "绿的谐波",
    "688385": "盟升电子", "002179": "中航光电", "601698": "中国卫通",
    "601998": "中信银行", "600486": "扬农化工", "688336": "三生国健",
    "688111": "金山办公", "300661": "圣邦股份", "688521": "芯原股份",
    "300782": "卓胜微", "688981": "中芯国际",
    "600016": "民生银行", "601009": "南京银行", "600000": "浦发银行",
    "601398": "工商银行", "601939": "建设银行", "601288": "农业银行",
    # v1.1 新增 — 报告中出现的其他标的
    "000408": "藏格矿业", "600988": "赤峰黄金", "688297": "中电港",
    "300699": "光威复材", "600893": "航发动力", "002179": "中航光电",
    "300073": "当升科技", "300450": "先导智能", "300316": "晶盛机电",
    "603290": "斯达半导", "300237": "美晨科技", "300487": "蓝晓科技",
    "600919": "江苏银行", "601128": "常熟银行", "600839": "四川长虹",
    "000625": "长安汽车", "601127": "赛力斯", "600433": "冠豪高新",
    "300433": "朗科智能", "002738": "中矿资源", "002842": "翔鹭钨业",
    "600362": "江西铜业", "600259": "广晟有色", "600111": "北方稀土",
    # v1.1 新增 — 军工/航天
    "600038": "中直股份", "600760": "中航沈飞", "600893": "航发动力",
    "000768": "中航西飞", "600150": "中国船舶", "601698": "中国卫通",
    "002049": "紫光国微", "600879": "航天电子", "600118": "中国卫星",
    # v1.1 新增 — 黄金产业链
    "600547": "山东黄金", "600489": "中金黄金", "600988": "赤峰黄金",
    "000975": "山金国际", "601899": "紫金矿业", "600311": "荣华实业",
    "600547": "山东黄金", "002155": "湖南黄金",
    # v1.1 新增 — 报告中其他
    "688521": "芯原股份", "300661": "圣邦股份", "688012": "中微公司",
    "688041": "海光信息", "300476": "胜宏科技", "300604": "长川科技",
    "300308": "中际旭创", "002407": "多氟多", "300398": "飞凯材料",
    "002371": "北方华创", "300142": "沃森生物", "603259": "药明康德",
    "002475": "立讯精密", "002415": "海康威视", "000725": "京东方A",
    "603160": "汇顶科技", "300782": "卓胜微", "300661": "圣邦股份",
    "688981": "中芯国际", "688012": "中微公司", "688041": "海光信息",
    # v1.1 新增 — 其他常见报告标的
    "600276": "恒瑞医药", "000538": "云南白药", "600436": "片仔癀",
    "000333": "美的集团", "000651": "格力电器", "600887": "伊利股份",
    "601888": "中国中免", "600036": "招商银行", "601318": "中国平安",
    "601628": "中国人寿", "600030": "中信证券", "601688": "华泰证券",
    "600837": "海通证券", "000776": "广发证券", "600999": "招商证券",
    "601169": "北京银行", "601009": "南京银行", "600000": "浦发银行",
    "601166": "兴业银行", "600016": "民生银行", "000001": "平安银行",
    # v1.1 新增 — 盛弘/航发/天味/蓝晓等
    "300692": "中环股份", "600893": "航发动力", "603027": "千禾味业",
    "603345": "安井食品", "600872": "中炬高新", "603288": "海天味业",
    "002714": "牧原股份", "002768": "国恩股份", "300487": "蓝晓科技",
    # v1.1.1 新增 — 四川黄金/湖南黄金等遗漏黄金股
    "000562": "四川黄金", "002155": "湖南黄金",
    # v1.1.1 新增 — 报告中其他遗漏标的
    "601919": "中远海控", "688005": "容百科技", "300662": "科沃斯",
    "688100": "威高信息", "300853": "申昊科技",
    "002916": "深南电路", "300768": "迪普科技",
    "688728": "格科微", "688526": "科前生物",
    # v1.5 新增 — 复盘/公众号高频漏网标的补全（2026-07-08 暴露：原边界规则+字典缺口双重漏混淆）
    "000977": "浪潮信息", "000938": "紫光股份", "600584": "长电科技",
    "603986": "兆易创新", "688525": "佰维存储", "002466": "天齐锂业",
    "002428": "云南锗业", "603019": "中科曙光", "601138": "工业富联",
    "300502": "新易盛", "002463": "沪电股份", "688256": "寒武纪",
    "688008": "澜起科技", "603501": "韦尔股份",
}

# 反向映射：名称→代码（v1.3 合并本地数据库 + 已知补充表）
# 本地数据库优先，已知补充表作为兜底
NAME_TO_CODE = {}

# 1. 先加载本地数据库（精确、无误判）
for name, code in LOCAL_NAME_TO_CODE.items():
    if name not in NAME_TO_CODE:
        NAME_TO_CODE[name] = code

# 2. 再加载已知补充表（仅补充数据库未覆盖项）
for code, name in KNOWN_STOCKS.items():
    if name not in NAME_TO_CODE:
        NAME_TO_CODE[name] = code

# v1.3 新增：本地数据库中的代码→名称映射（用于精确代码检测）
CODE_TO_NAME = dict(LOCAL_CODE_TO_NAME)
for code, name in KNOWN_STOCKS.items():
    if code not in CODE_TO_NAME:
        CODE_TO_NAME[code] = name

# 注意：v1.1-v1.2 使用的动态后缀检测（COMPANY_SUFFIXES / find_dynamic_stock_names）
# 已在 v1.3 中移除。检测逻辑完全基于本地沪深A股数据库 + 已知补充表的精确匹配。

# ============================================================
# 规则 3：英文策略名称→中文对照表
# ============================================================
STRATEGY_CN_MAP = {
    # 9种核心策略
    "dragonHead": "龙头战法",
    "sparrow": "麻雀战法",
    "turtle": "海龟战法",
    "bollingerReversion": "布林回归",
    "gridTrading": "网格交易",
    "maCross": "均线交叉",
    "volumeBreakout": "量价突破",
    "trendAcceleration": "趋势加速",
    "highGrowth": "高增长策略",
    # 发现层策略
    "hotSector": "热门板块发现",
    # 市态名称
    "mainRally": "主升浪",
    "oscillatingUp": "震荡上行",
    "healthyPullback": "健康回调",
    "boxRange": "箱体震荡",
    "oscillatingDown": "震荡下行",
    "trendReversal": "行情反转",
    "crashMode": "急跌崩盘",
    # 轨道名称
    "valueTrack": "价值轨",
    "trendTrack": "趋势轨",
    "hotSectorTrack": "热门板块轨",
    # 其他术语
    "activeStrategy": "当前策略",
    "marketRegime": "市态判定",
    "bullBear": "牛熊判定",
    "subState": "子市态",
    "strategyConfidence": "策略置信度",
    "styleRotation": "风格轮动",
    "marketStyle": "市场风格",
}


# ============================================================
# 规则 4：英文术语/参数名/表头→中文对照表（v1.1新增）
# ============================================================
ENGLISH_TERM_MAP = {
    # westock-tool 参数名
    "boll_bt_mid": "布林中轨",
    "boll_bt_upper": "布林上轨",
    "boll_bt_lower": "布林下轨",
    "ma_stick": "均线粘合",
    "pb_roe": "低估值高回报",
    "profit_preannounce": "业绩预增",
    "kdj_golden": "KDJ金叉",
    "rise_big_up": "大涨突破",
    "macd_golden": "MACD金叉",
    "one_rise_three_ma": "一阳穿三线",
    "bollingerReversion": "布林回归",
    # 工具名
    "westock-tool": "选股工具",
    "westock-data": "行情数据",
    "westock": "选股",  # 单独出现时的翻译（如"对应westock策略"→"对应选股策略"）
    "mx-xuangu": "交叉验证",
    "mx-data": "金融数据",
    "mx-moni": "模拟交易",
    "mx-poster": "社区发帖",
    # 表头英文
    "styleTag": "风格标签",
    "confidence": "置信度",
    "buyCondition": "买入条件",
    "bull": "多头",
    "bear": "空头",
    # 其他常见英文
    "filter": "筛选",
    "score": "评分",
    "watching": "观察中",
    "holding": "持仓中",
    "stopLoss": "止损",
    "takeProfit": "止盈",
    "priceRange": "价格区间",
    "signalRequired": "信号条件",
    "plannedPosition": "计划仓位",
    "perStockMax": "单股上限",
    "maxPositions": "最大持仓数",
    "riskBudget": "风险预算",
    "availableBudget": "可用预算",
    "usedBudget": "已用预算",
    "allocation": "配置比例",
    "budget": "预算",
    "sources": "来源",
    "priority": "优先级",
    "reason": "选股理由",
    "status": "状态",
    "code": "代码",
    "name": "名称",
    "note": "备注",
    "weeklyReviewAt": "周度复审",
    "addedAt": "加入日期",
    "oscillatingDownNote": "震荡下行备注",
    "subStateConfidence": "子态置信度",
    "bullBearConfidence": "牛熊置信度",
    "lastUpdated": "最后更新",
    "rotationCount5d": "5日轮动次数",
    "protectionLevel": "防护等级",
    "positionLimit": "仓位限制",
    "maxTotalPosition": "最大总仓位",
    "strategyAllocation": "策略配置",
    "activeStrategies": "激活策略",
    "hotSectorLog": "热门板块日志",
    "newsDiscoveryLog": "热点资讯日志",
    "auctionDiscoveryLog": "竞价发现日志",
    "regimeNote": "市态备注",
    "styleRotation": "风格轮动",
    "marketRegime": "市态判定",
    "systemVersion": "系统版本",
    "initialCapital": "初始资金",
    "updatedAt": "更新时间",
    "accountId": "账户ID",
    "account": "账户",
    # PE/PB/ROE 等保留但加注
    # 这些在 simplify_technical_content 中处理
}


def replace_english_terms(text: str) -> str:
    """将英文术语/参数名/表头替换成中文（v1.1新增）。
    
    匹配规则：
    - 独立单词（前后是非字母字符或行首行尾）
    - 不替换出现在代码块内的
    - 按术语名长度降序匹配（长的先匹配）
    """
    # 收集所有代码块范围
    code_block_ranges = []
    for m in re.finditer(r'(```[\s\S]*?```|`[^`]+`)', text):
        code_block_ranges.append((m.start(), m.end()))
    
    def is_in_code_block(pos: int) -> bool:
        for start, end in code_block_ranges:
            if start <= pos < end:
                return True
        return False
    
    # 按术语名长度降序排序
    sorted_terms = sorted(ENGLISH_TERM_MAP.keys(), key=len, reverse=True)
    
    # 构建合并正则
    pattern_parts = [re.escape(term) for term in sorted_terms]
    combined_pattern = r'(?<![a-zA-Z_-])(' + '|'.join(pattern_parts) + r')(?![a-zA-Z_-])'
    
    # 一次性找到所有匹配
    matches = []
    for m in re.finditer(combined_pattern, text):
        if not is_in_code_block(m.start()):
            eng_term = m.group(1)
            cn_term = ENGLISH_TERM_MAP[eng_term]
            matches.append((m.start(1), m.end(1), cn_term))
    
    # 从后往前替换
    result = text
    for start, end, cn_term in reversed(matches):
        result = result[:start] + cn_term + result[end:]
    
    return result


def replace_strategy_names(text: str) -> str:
    """将英文策略名称替换成中文。
    
    匹配规则：
    - 独立单词（前后是非字母字符）
    - 不替换出现在代码块内的（保留技术准确性）
    
    修复：一次性收集所有匹配位置，从后往前替换避免位置偏移问题。
    """
    # 收集所有代码块范围
    code_block_ranges = []
    for m in re.finditer(r'(```[\s\S]*?```|`[^`]+`)', text):
        code_block_ranges.append((m.start(), m.end()))
    
    def is_in_code_block(pos: int) -> bool:
        for start, end in code_block_ranges:
            if start <= pos < end:
                return True
        return False
    
    # 按策略名长度降序排序（长的先匹配，避免短名吃掉长名的前缀）
    sorted_strategies = sorted(STRATEGY_CN_MAP.keys(), key=len, reverse=True)
    
    # 构建合并正则
    pattern_parts = []
    for eng_name in sorted_strategies:
        pattern_parts.append(re.escape(eng_name))
    
    combined_pattern = r'(?<![a-zA-Z])(' + '|'.join(pattern_parts) + r')(?![a-zA-Z])'
    
    # 一次性找到所有匹配
    matches = []
    for m in re.finditer(combined_pattern, text):
        if not is_in_code_block(m.start()):
            eng_name = m.group(1)
            cn_name = STRATEGY_CN_MAP[eng_name]
            matches.append((m.start(), m.end(), cn_name))
    
    # 从后往前替换
    result = text
    for start, end, cn_name in reversed(matches):
        result = result[:start] + cn_name + result[end:]
    
    return result


# ============================================================
# 综合润色流程
# ============================================================

def find_and_obfuscate_stocks(text: str) -> str:
    """扫描文本中的所有股票代码和公司名称，进行混淆。
    
    v1.3改进：
    1. 基于本地沪深A股数据库（5198+只）精确匹配公司名称
    2. 仅对数据库中存在的6位数字股票代码进行混淆
    3. 移除动态后缀检测，避免普通文字被误判为公司名
    """
    
    # 收集代码块范围
    code_block_ranges = []
    for m in re.finditer(r'(```[\s\S]*?```|`[^`]+`)', text):
        code_block_ranges.append((m.start(), m.end()))
    
    def is_in_code_block(pos: int) -> bool:
        for start, end in code_block_ranges:
            if start <= pos < end:
                return True
        return False
    
    # --- Step 1: 混淆公司名称（精确匹配本地数据库 + 补充表）---
    # v1.3 边界规则原为「公司名前后不能是中文字符」，但中文行文里公司名必被汉字/标点
    # 包围，导致正常语境下的公司名几乎全部漏混淆（合规漏洞，2026-07-08 复盘暴露）。
    # v1.5 修复：按名称长度分级处理 ——
    #   * 长名称(>=4字，如“浪潮信息/赤峰黄金”)：放宽边界，仅阻止与字母数字相邻
    #     （避免误匹配代码段如 600519XX），允许汉字/标点相邻 → 正常语境均可命中。
    #   * 短名称(<=3字，如“机器人/银行”)：保留严格汉字边界，避免“人形机器人”误匹配“机器人”。
    sorted_names = sorted(NAME_TO_CODE.keys(), key=len, reverse=True)
    long_names = [re.escape(_n) for _n in sorted_names if len(_n) >= 4]
    short_names = [re.escape(_n) for _n in sorted_names if len(_n) <= 3]

    # 长名(>=4字)：放宽边界，仅阻止与字母数字相邻（避免误匹配代码段）
    # 短名(<=3字)：保留严格汉字边界，避免“人形机器人”误匹配“机器人”
    _pattern_parts = []
    if long_names:
        _pattern_parts.append(r'(?<![0-9a-zA-Z])(' + '|'.join(long_names) + r')(?![0-9a-zA-Z])')
    if short_names:
        _pattern_parts.append(r'(?<![\u4e00-\u9fff])(' + '|'.join(short_names) + r')(?![\u4e00-\u9fff])')
    name_pattern = '|'.join(_pattern_parts)

    name_matches = []
    for m in re.finditer(name_pattern, text):
        if not is_in_code_block(m.start()):
            original_name = m.group(1) or m.group(2)
            obfuscated = obfuscate_company_name(original_name)
            # 取实际匹配的 group 边界
            gidx = 1 if m.group(1) else 2
            name_matches.append((m.start(gidx), m.end(gidx), obfuscated))
    
    # 从后往前替换
    for start, end, obfuscated in reversed(name_matches):
        text = text[:start] + obfuscated + text[end:]
    
    # --- Step 2: 混淆6位纯数字股票代码（仅数据库中存在的代码）---
    def replace_code(m):
        code = m.group(0)
        if len(code) == 6 and code.isdigit() and code in CODE_TO_NAME:
            return obfuscate_stock_code(code)
        return code
    
    text = re.sub(r'(?<!\d)(\d{6})(?!\d)', replace_code, text)
    
    return text


def add_copyright(text: str, position: str = "end") -> str:
    """添加版权声明。
    
    position: "end" (文末) 或 "start" (文首)
    """
    if position == "start":
        lines = text.split('\n')
        insert_pos = 0
        for i, line in enumerate(lines):
            if line.startswith('#'):
                insert_pos = i + 1
                break
        
        lines.insert(insert_pos, COPYRIGHT_MD_PREFIX.strip())
        return '\n'.join(lines)
    else:
        return text + COPYRIGHT_TEMPLATE


def simplify_technical_content(text: str) -> str:
    """将过于技术的表述简化为通俗表述。
    
    主要处理：
    - "MA5/MA10/MA20" → "5日均线/10日均线/20日均线"
    - "MACD金叉/死叉" → 加通俗解释
    - "ATR×N" → "波动止损"
    - "布林带" → 加通俗解释
    - "PE/PB/ROE" → 加通俗注解（首次出现时）
    """
    result = text
    
    # 英文估值/财务指标缩写→中文（v1.5 新增 · 三段顺序第①步通俗化兜底）
    # 仅匹配独立大写缩写，避免误伤组合词；保留原缩写便于专业读者对照；
    # 负向后顾排除 "("，防止重复运行对已包裹的 "市盈率(PE)" 二次包裹。
    result = re.sub(r'(?<![A-Za-z\u4e00-\u9fff(])PE(?![A-Za-z])', '市盈率(PE)', result)
    result = re.sub(r'(?<![A-Za-z\u4e00-\u9fff(])ROE(?![A-Za-z])', '净资产收益率(ROE)', result)
    result = re.sub(r'(?<![A-Za-z\u4e00-\u9fff(])PB(?![A-Za-z])', '市净率(PB)', result)
    result = re.sub(r'(?<![A-Za-z\u4e00-\u9fff(])EPS(?![A-Za-z])', '每股收益(EPS)', result)
    result = re.sub(r'(?<![A-Za-z\u4e00-\u9fff(])PS(?![A-Za-z])', '市销率(PS)', result)
    
    # MA均线通俗化
    result = re.sub(r'MA5', '5日均线', result)
    result = re.sub(r'MA10', '10日均线', result)
    result = re.sub(r'MA20', '20日均线', result)
    result = re.sub(r'MA60', '60日均线', result)
    result = re.sub(r'MA(\d+)', r'\1日均线', result)
    
    # MACD通俗化（避免与英文术语表重复处理）
    result = re.sub(r'MACD金叉（看涨信号）', 'MACD金叉（看涨信号）', result)  # 避免重复
    result = re.sub(r'(?<!）)MACD金叉(?!（)', 'MACD金叉（看涨信号）', result)
    result = re.sub(r'(?<!）)MACD死叉(?!（)', 'MACD死叉（看跌信号）', result)
    
    # ATR通俗化（仅在正文描述中，代码块内保留）
    result = re.sub(r'ATR×(\d+\.?\d*)', r'波动止损（ATR×\1）', result)
    
    # 布林带通俗化（避免重复处理）
    result = re.sub(r'布林带下轨（超卖区）', '布林带下轨（超卖区）', result)
    result = re.sub(r'布林带上轨（超买区）', '布林带上轨（超买区）', result)
    result = re.sub(r'(?<!（)布林下轨(?!（)', '布林带下轨（超卖区）', result)
    result = re.sub(r'(?<!（)布林上轨(?!（)', '布林带上轨（超买区）', result)
    result = re.sub(r'(?<!（)布林中轨(?!（)', '布林带中轨', result)
    
    # bollinger_lower/bollinger_upper 通俗化
    result = re.sub(r'boll_lower', '布林带下轨', result)
    result = re.sub(r'boll_upper', '布林带上轨', result)
    
    return result


def deduplicate_translations(text: str) -> str:
    """去除英文术语翻译后产生的连续重复中文词（v1.1.1新增）。
    
    例："交叉验证交叉验证" → "交叉验证"
    例："选股工具选股工具" → "选股工具"
    
    只处理2-6字中文词的连续重复，避免误删正常文本。
    """
    # 匹配连续重复的2-6字中文词
    # (?<![\u4e00-\u9fff]) 确保前面不是中文（避免匹配到更长的词）
    # ([\u4e00-\u9fff]{2,6})  2-6字中文词
    # \1  相同的词重复一次
    result = re.sub(r'(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,6})\1(?![\u4e00-\u9fff])', r'\1', text)
    return result


def fix_cjk_emphasis(text: str) -> str:
    """修复中文语境下 Markdown 强调语法失效的问题（v1.5 新增）。

    ── 问题 ──
    CommonMark 的 left/right-flanking delimiter run 规则规定：
    `**` 左邻普通字符、右邻 Unicode 标点时，不构成「强调开启符」。
    中文写作里 `今天是**「碳基跌倒」**`、`这是**（重点）**`、`看**"这里"**`
    这类紧贴中文标点的写法会整体失效，公众号里原样显示星号。

    ── 修复 ──
    渲染前直接把强调语法转成内联 HTML，绕开定界符规则。
    实测 wenyan 会给内联 <strong>/<em> 施加与 Markdown 强调完全相同的
    主题样式（color: rgb(72,112,172)），视觉零差异；微信编辑器同样支持。

    围栏代码块、行内代码、已有 HTML 标签内的内容一律不受影响。
    """
    placeholders: list = []

    def _stash(m):
        placeholders.append(m.group(0))
        return f"\x00CBEM{len(placeholders) - 1}\x00"

    # 1) 保护：围栏代码块 → 行内代码 → 已有 HTML 标签
    guarded = re.sub(r'```[\s\S]*?```', _stash, text)
    guarded = re.sub(r'~~~[\s\S]*?~~~', _stash, guarded)
    guarded = re.sub(r'`[^`\n]+`', _stash, guarded)
    guarded = re.sub(r'<[^<>\n]+>', _stash, guarded)

    # 2) 强调语法 → 内联 HTML（限定单行内，内容首尾非空白）
    guarded = re.sub(r'\*\*\*(?=\S)([^\n]+?)(?<=\S)\*\*\*',
                     r'<strong><em>\1</em></strong>', guarded)
    guarded = re.sub(r'\*\*(?=\S)([^\n]+?)(?<=\S)\*\*',
                     r'<strong>\1</strong>', guarded)
    guarded = re.sub(r'(?<![\w*])__(?=\S)([^\n]+?)(?<=\S)__(?![\w*])',
                     r'<strong>\1</strong>', guarded)
    # 单星号斜体：前后不得是单词字符或星号，避免误伤列表符号 "* " 与乘号 a*b
    guarded = re.sub(r'(?<![\w*])\*(?=[^\s*])([^\n*]+?)(?<=[^\s*])\*(?![\w*])',
                     r'<em>\1</em>', guarded)

    # 3) 还原被保护的片段
    return re.sub(r'\x00CBEM(\d+)\x00',
                  lambda m: placeholders[int(m.group(1))], guarded)


def transform_for_wechat(input_text: str, title: str = None, add_frontmatter: bool = True,
                         no_obfuscate: bool = False) -> str:
    """完整的公众号内容处理流程（三段顺序铁律：①润色 → ②后处理混淆 → ③发布）。

    润色类（让文章通俗易懂，对应三段顺序第①步）：
    1. 英文策略名→中文
    2. 英文术语/参数名→中文（v1.1新增）
    3. 技术术语通俗化
    4. 去除英文术语翻译后产生的连续重复中文词（v1.1.1新增）
    后处理（代码/名称混淆，对应三段顺序第②步，必须后于润色执行）：
    5. 个股代码/名称混淆（v1.3改为本地数据库精确匹配）— 可选，no_obfuscate=True 时跳过
    收尾：
    6. 版权声明
    7. Frontmatter（可选）
    """
    result = input_text
    
    # ===== 润色类：先把文章改得通俗易懂（对应三段顺序第①步）=====
    # Step 1: 英文策略名替换
    result = replace_strategy_names(result)
    
    # Step 2: 英文术语/参数名/表头替换（v1.1新增）
    result = replace_english_terms(result)
    
    # Step 3: 技术术语通俗化
    result = simplify_technical_content(result)
    
    # Step 4: 去除英文术语翻译后产生的连续重复中文词（v1.1.1新增）
    result = deduplicate_translations(result)
    
    # ===== 后处理：再做代码/名称混淆（对应三段顺序第②步，必须后于润色）=====
    # Step 5: 个股代码和名称混淆（v1.3 本地数据库精确匹配）
    # v1.4: 支持 --no-obfuscate 跳过混淆，用于故事类文章保留真实股票名
    if not no_obfuscate:
        result = find_and_obfuscate_stocks(result)
    
    # ===== 收尾 =====
    # Step 6: 版权声明（文首声明）
    result = add_copyright(result, position="start")
    
    # Step 6.5: 修复中文标点旁的强调语法失效（v1.5新增，必须在 frontmatter 之前）
    #           **「xxx」** 这类写法不符合 CommonMark flanking 规则，
    #           不转 HTML 的话星号会原样漏到公众号正文里
    result = fix_cjk_emphasis(result)
    
    # Step 6: Frontmatter
    if add_frontmatter:
        # 先去掉原有的frontmatter（如果有），避免frontmatter干扰标题提取（v1.3修复）
        result = re.sub(r'^---[\s\S]*?---\n', '', result)
        result = result.lstrip('\n')
        
        if title is None:
            m = re.search(r'^#\s+(.+)', result)
            title = m.group(1) if m else "云侠AI量化交易日报"
        
        # YAML frontmatter: 移除 title 中所有类型的引号（ASCII + 中文/弯引号），防止 YAML 嵌套引号解析失败
        title = title.replace('\u201c', '').replace('\u201d', '').replace('\u0022', '').replace('\u300c', '').replace('\u300d', '')
        
        cover = "./assets/default-cover.jpg"
        
        frontmatter = f"""---
title: "{title}"
cover: "{cover}"
---

"""
        result = frontmatter + result
    
    return result


# ============================================================
# 命令行入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='云侠公众号内容润色脚本 v1.3')
    parser.add_argument('input', help='输入Markdown文件路径')
    parser.add_argument('output', help='输出Markdown文件路径')
    parser.add_argument('--title', help='文章标题（默认从文件中提取）')
    parser.add_argument('--cover', help='封面图路径（默认 ./assets/default-cover.jpg）')
    parser.add_argument('--no-frontmatter', action='store_true', help='不添加frontmatter')
    parser.add_argument('--no-obfuscate', action='store_true', default=False,
                        help='不混淆股票代码和公司名称（用于故事类文章保留真实名称）')
    
    args = parser.parse_args()
    
    # 读取输入文件
    if not os.path.exists(args.input):
        print(f"❌ 输入文件不存在: {args.input}")
        sys.exit(1)
    
    with open(args.input, 'r', encoding='utf-8') as f:
        input_text = f.read()
    
    # 执行润色
    result = transform_for_wechat(
        input_text,
        title=args.title,
        add_frontmatter=not args.no_frontmatter,
        no_obfuscate=args.no_obfuscate
    )
    
    # 如果指定了cover，替换默认cover
    if args.cover:
        result = result.replace('./assets/default-cover.jpg', args.cover)
    
    # 确保输出目录存在
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    # 也确保assets目录存在（封面图可能需要）
    article_dir = os.path.dirname(os.path.abspath(args.output))
    assets_dir = os.path.join(article_dir, 'assets')
    os.makedirs(assets_dir, exist_ok=True)
    
    # 写入输出文件
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(result)
    
    print(f"✅ 润色完成！输出文件: {args.output}")
    print(f"   - 策略名中文化: 已完成")
    print(f"   - 英文术语中文化: 已完成（v1.1新增）")
    if args.no_obfuscate:
        print(f"   - 个股代码/名称混淆: 已跳过（--no-obfuscate）")
    else:
        print(f"   - 个股代码/名称混淆: 已完成（v1.3 本地数据库精确匹配 {len(LOCAL_CODE_TO_NAME)} 只A股）")
    print(f"   - 技术术语通俗化: 已完成")
    print(f"   - 版权声明: 已添加")
    print(f"   - Frontmatter: {'已添加' if not args.no_frontmatter else '已跳过'}")


if __name__ == '__main__':
    main()
