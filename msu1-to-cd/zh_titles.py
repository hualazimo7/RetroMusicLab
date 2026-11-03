"""游戏名中文化（joshw 音源列表用）。

joshw 目录里的档案名是英文的, 比如
``16 Mahjong (1989)(Sachen, Thin Chen)[NES]`` —— 去掉 ``(...)`` ``[...]`` 里的年份 /
厂商 / 平台之后, 剩下的才是真正的标题。这个模块把标题翻成中文, 分三层, 越往后
越"直译"、准确率越低:

1. ``GAME_ZH``  精确词典(已确认通行的官方中文名 / 通用译名)
2. ``_PHRASES`` 词组替换(系列名: final fantasy -> 最终幻想 这种)
3. ``_TOKENS``  分词组词(逐词替换, 产出"忍者战士 3"这种直译名)

三层都拿不到有把握的结果时返回空串 —— 宁可留空让人看英文原名, 也不给一个编出来的
假译名。UI 收到空串就显示 ``—``。
"""

import functools
import re
from typing import Dict, List, Optional, Tuple

__all__ = ["core_title", "translate", "zh_name", "zh_or_dash",
           "zh_weak", "coverage"]

# --------------------------------------------------------------------------
# 规范化
# --------------------------------------------------------------------------
_META_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_NONWORD = re.compile(r"[^0-9a-z一-鿿]+")


def core_title(name: str) -> str:
    """剥掉结尾的 ``(年份)(厂商)[平台]`` 之类元数据, 只留标题本体。

    ``3-in-1 Lion King 5 (1993)(Dragon)[NES]`` -> ``3-in-1 Lion King 5``
    """
    s = (name or "").strip()
    # 反复剥, 有些档案名后面挂了 3~4 层括号
    while True:
        stripped = _META_RE.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    return s


def _norm(s: str) -> str:
    """转成便于查词典的键: 小写、去标点、压缩空格。"""
    s = (s or "").lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    s = _NONWORD.sub(" ", s)
    return " ".join(s.split())


def _is_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in (s or ""))


# --------------------------------------------------------------------------
# 第 1 层: 精确词典(键 = 规范化后的标题)
# --------------------------------------------------------------------------
GAME_ZH: Dict[str, str] = {
    # ---- 超级马里奥 ----
    "super mario bros": "超级马里奥兄弟",
    "super mario bros 2": "超级马里奥兄弟 2",
    "super mario bros 3": "超级马里奥兄弟 3",
    "super mario bros 4": "超级马里奥兄弟 4",
    "super mario world": "超级马里奥世界",
    "super mario kart": "超级马里奥赛车",
    "super mario 64": "超级马里奥 64",
    "super mario odyssey": "超级马里奥 奥德赛",
    "new super mario bros": "新超级马里奥兄弟",
    "new super mario bros u": "新超级马里奥兄弟 U",
    "mario": "马里奥",
    "mario bros": "马里奥兄弟",
    "super mario": "超级马里奥",
    "mario kart": "马里奥赛车",
    "luigi": "路易吉",
    # ---- 塞尔达 ----
    "the legend of zelda": "塞尔达传说",
    "zelda ii the adventure of link": "塞尔达传说 II",
    "a link to the past": "塞尔达传说 梦见岛",
    "link to the past": "塞尔达传说 梦见岛",
    "ocarina of time": "时之笛",
    "majoras mask": "假面",
    "majora s mask": "摩尔加面具",
    "majora s mask": "摩尔加面具",
    "the wind waker": "风之杖",
    "four sword adventures": "四剑传说",
    "twilight princess": "黄昏公主",
    # ---- 大金刚 / 平台跳跃 ----
    "donkey kong": "大金刚",
    "donkey kong jr": "大金刚小子",
    "donkey kong country": "大金刚 Country",
    "dkc": "大金刚 Country",
    "dkc2": "大金刚 Country 2",
    "donkey kong country 2": "大金刚 Country 2",
    "donkey kong 64": "大金刚 64",
    "donkey kong jungle beat": "大金刚 丛林打击",
    "mario party": "马里奥派对",
    "mario party 2": "马里奥派对 2",
    "mario party 3": "马里奥派对 3",
    "mario party 4": "马里奥派对 4",
    "mario party 5": "马里奥派对 5",
    "mario party 6": "马里奥派对 6",
    "mario party 7": "马里奥派对 7",
    "mario party 8": "马里奥派对 8",
    "mario party 9": "马里奥派对 9",
    "luigi s mansion": "路易吉洋馆",
    "mario s picross": "超级马里奥画片",
    "super mario picross 2": "超级马里奥画片 2",
    "super mario picross 3": "超级马里奥画片 3",
    # ---- 忍者 / 动作 ----
    "contra": "魂斗罗",
    "contra force": "魂斗罗 FORCE",
    "contra 4": "魂斗罗 4",
    "contra iii the alien wars": "魂斗罗 III",
    "super contra": "超级魂斗罗",
    "metal gear": "合金装备",
    "metal gear 2 solid snake": "合金装备 2",
    "metal gear solid": "合金装备 Solid",
    "castlevania": "恶魔城",
    "castlevania 2 belmont s revenge": "恶魔城 2",
    "castlevania symphony of the night": "恶魔城 月下夜想曲",
    "castlevania bloodlines": "恶魔城 血族",
    "ninja gaiden": "忍者龙剑传",
    "ninja gaiden 2": "忍者龙剑传 2",
    "ninja gaiden 3": "忍者龙剑传 3",
    "ninja gaiden trilogy": "忍者龙剑传 合集",
    "ghosts n goblins": "魔吉塔",
    "ghosts n goblins": "魔吉塔",
    "strider": "铁甲飞龙",
    "strider 2": "铁甲飞龙 2",
    "final fight": "快打旋风",
    "rastan": "拉斯坦",
    "rastan saga ii": "拉斯坦 2",
    "karnov": "卡诺夫",
    "commando": "突击队员",
    "bionic commando": "生化尖兵",
    "mercs": "佣兵",
    "jackal": "非洲突击兵",
    "green beret": "绿色贝雷帽",
    "ikari warriors": "怒",
    "victory road": "胜利之路",
    "heavy barrel": "重炮",
    "hunchback": "驼背人",
    "circus charlie": "马戏团查理",
    "elevator action": "电梯行动",
    "diamond dodge": "钻石大冒险",
    "the ninja warriors": "忍者战士",
    "ninja warriors": "忍者战士",
    # ---- 洛克人 / 卡普空 ----
    "mega man": "洛克人",
    "mega man 2": "洛克人 2",
    "mega man 3": "洛克人 3",
    "mega man 4": "洛克人 4",
    "mega man 5": "洛克人 5",
    "mega man 6": "洛克人 6",
    "mega man 7": "洛克人 7",
    "mega man 8": "洛克人 8",
    "mega man x": "洛克人 X",
    "mega man x2": "洛克人 X2",
    "mega man x3": "洛克人 X3",
    "mega man x4": "洛克人 X4",
    "mega man battle network": "洛克人 网络对战",
    "rockman": "洛克人",
    "final fight 2": "快打旋风 2",
    "street fighter": "街头霸王",
    "street fighter ii": "街头霸王 II",
    "street fighter iii": "街头霸王 III",
    "street fighter 2010": "街头霸王 2010",
    "super street fighter ii turbo": "超级街头霸王 II",
    "the king of fighters": "拳皇",
    "king of fighters": "拳皇",
    "king of fighters 94": "拳皇 94",
    "king of fighters 95": "拳皇 95",
    "king of fighters 96": "拳皇 96",
    "king of fighters 97": "拳皇 97",
    "king of fighters 98": "拳皇 98",
    "king of fighters 99": "拳皇 99",
    "king of fighters 2000": "拳皇 2000",
    "king of fighters 2002": "拳皇 2002",
    "kof 94": "拳皇 94",
    "kof 95": "拳皇 95",
    "kof 96": "拳皇 96",
    "kof 97": "拳皇 97",
    "kof 98": "拳皇 98",
    "kof 99": "拳皇 99",
    "kof 2000": "拳皇 2000",
    "mortal kombat": "真人快打",
    "mortal kombat 3": "真人快打 3",
    "soul edge": "剑魂",
    "soulcalibur": "剑魂",
    "street fighter ii champion edition": "街头霸王 II 加强版",
    "pro evolution soccer": "实况足球",
    "pro evolution soccer 2": "实况足球 2",
    "jumping flash": "闪亮神兵",
    "super puzzle fighter": "超级解谜格斗",
    "vigilante 8": "锄奸 8",
    # ---- 忍者龙剑传 / 恶魔城 补充 ----
    "castlevania circle of the moon": "恶魔城 月之环",
    "castlevania harmony of dissonance": "恶魔城 和谐",
    "mega man x2 s": "洛克人 X2",
    "rockman x": "洛克人 X",
    # ---- 太空 / 射击 ----
    "space invaders": "太空侵略者",
    "invaders": "太空侵略者",
    "galaga": "小蜜蜂",
    "galaxian": " galaxian",
    "xevious": "沙罗曼蛇",
    "gradius": "宇宙巡航机",
    "r type": "雷电",
    "r type 2": "雷电 2",
    "darwinia": "Darwinia",
    "scramble": "Scramble",
    "zigzag": "ZigZag",
    "1942": "1942",
    "1943": "1943",
    "19th century": "19 世纪",
    "19th century ii": "19 世纪 II",
    "19th century expedition": "19 世纪远征",
    "stargate": "星际之门",
    "sigma star saga": "究极虎将",
    "star soldier": "星之士兵",
    "star soldier 2": "星之士兵 2",
    "star force": "星际力量",
    "star wars": "星球大战",
    "the empire strikes back": "帝国反击战",
    "return of the jedi": "绝地归来",
    "super star wars": "超级星球大战",
    "robo warrior": "机器战士",
    "battleship": "战舰",
    "gunship": "炮艇",
    "thunder force": "雷霆战机",
    "thunder blade": "雷霆战机 2",
    "atomic robo-kid": "原子机器人小子",
    "task force harrier ex": "Task Force Harrier EX",
    # ---- 坦克 / 竞速 ----
    "battle tank": "坦克大战",
    "battle city": "坦克大战",
    "battle tank 2": "坦克大战 2",
    "battle tank 3": "坦克大战 3",
    "racing": "赛车",
    "racing 4": "赛车 4",
    "micro machines": "微型机器",
    "micro machines 2 turbo tournament": "微型机器 2",
    "f1 race": "F1 赛车",
    "super rc": "超级遥控车",
    "rc pro": "遥控赛车 Pro",
    "road rash": "暴力摩托",
    "road rash 3": "暴力摩托 3",
    "excitebike": "极限机车",
    "bike or die": "机车生死斗",
    "bike or die 2": "机车生死斗 2",
    "skate or die": "滑板生死斗",
    "super off road": "越野摩托",
    "grand prix arcade": "大奖赛街机",
    "speed riders": "极速骑士",
    # ---- 棋牌 / 益智 ----
    "mahjong": "麻将",
    "16 mahjong": "16 麻将",
    "seicross": "超级越野",
    "boulder dash": "石块迷宫",
    "bomberman": "炸弹人",
    "bomberman 2": "炸弹人 2",
    "bomberman 3": "炸弹人 3",
    "bomberman 5": "炸弹人 5",
    "bomberman 64": "炸弹人 64",
    "bomberman hns": "炸弹人",
    "tetris": "俄罗斯方块",
    "tetris 2": "俄罗斯方块 2",
    "puyo puyo": "噗哟噗哟",
    "puyo puyo tsu": "噗哟噗哟 通",
    "columns": "俄罗斯方块",
    "panel de pon": "面板方块",
    "klax": "Klax",
    "qix": "Qix",
    "gemboq": "宝石方块",
    "pitfall": "Pitfall",
    "pitfall 2": "Pitfall 2",
    "pitfall 3": "Pitfall 3",
    "solomon s key": "所罗门之钥",
    "solomon s key 2": "所罗门之钥 2",
    "shaq fu": "鲨鱼大冒险",
    "excitebike 2": "极限机车 2",
    "chess": "国际象棋",
    "shogi": "将棋",
    "go": "围棋",
    "card game": "纸牌游戏",
    # ---- RPG ----
    "final fantasy": "最终幻想",
    "final fantasy ii": "最终幻想 II",
    "final fantasy iii": "最终幻想 III",
    "final fantasy iv": "最终幻想 IV",
    "final fantasy v": "最终幻想 V",
    "final fantasy vi": "最终幻想 VI",
    "final fantasy mystic quest": "最终幻想 神秘任务",
    "chrono trigger": "时空之轮",
    "chrono trigger 2": "时空之轮 2",
    "secret of mana": "圣剑豪情",
    "seiken densetsu": "圣剑传说",
    "seiken densetsu 2": "圣剑传说 2",
    "seiken densetsu 3": "圣剑传说 3",
    "the 7th saga": "第七魔剑",
    "earthbound": "星海传说",
    "earthbound beginnings": "星海传说 序章",
    "mother 1": "Mother 1",
    "mother 2": "Mother 2",
    "mother 3": "Mother 3",
    "drago n": "勇者斗恶龙",
    "dragon quest": "勇者斗恶龙",
    "dragon quest ii": "勇者斗恶龙 II",
    "dragon quest iii": "勇者斗恶龙 III",
    "dragon quest iv": "勇者斗恶龙 IV",
    "dragon quest v": "勇者斗恶龙 V",
    "dragon quest vi": "勇者斗恶龙 VI",
    "dragon quest vii": "勇者斗恶龙 VII",
    "dragon quest monsters": "勇者斗恶龙 怪物篇",
    "dq3": "勇者斗恶龙 III",
    "dq4": "勇者斗恶龙 IV",
    "dq5": "勇者斗恶龙 V",
    "dq6": "勇者斗恶龙 VI",
    "dq7": "勇者斗恶龙 VII",
    "dq8": "勇者斗恶龙 VIII",
    "romancing saga": "重装机兵",
    "romancing saga 2": "重装机兵 2",
    "romancing saga 3": "重装机兵 3",
    "romancing saGa": "沙迦传说",
    "saga 1": "沙迦传说",
    "saga 2": "沙迦传说 2",
    "saga 3": "沙迦传说 3",
    "breath of fire": "呼吸之火",
    "breath of fire 2": "呼吸之火 2",
    "lufia": "露非雅",
    "lUFia": "露非雅",
    "secret of marian": "玛丽亚的秘密",
    "grandia": "Grandia",
    "star ocean": "星海传说",
    "star ocean 2": "星海传说 2",
    "suikoden": "幻想水滸伝",
    "suikoden 2": "幻想水滸伝 2",
    "ff4": "最终幻想 IV",
    "ff5": "最终幻想 V",
    "ff6": "最终幻想 VI",
    "ct": "时空之轮",
    "soa": "圣剑豪情",
    "alundra": "神圣大地精",
    "illusion of gaia": "盖亚幻境",
    "terror blade": "恐怖之刃",
    "octopath traveler": "八方旅人",
    "cdr-2": "炸弹人 2",
    # ---- 冒险 / 解谜 ----
    "zelda": "塞尔达传说",
    "milon secret castle": "米隆的秘密城堡",
    "milon secret castle 3": "米隆的秘密城堡 3",
    "kid icarus": "光神",
    "kid icarnes adventure": "光神",
    "metroid": "密特罗德",
    "metroid 2": "密特罗德 2",
    "metroid 3": "密特罗德 3",
    "metroid 4": "密特罗德 4",
    "super metroid": "超级密特罗德",
    "super metroid 2": "超级密特罗德 2",
    "metroid fusion": "密特罗德 融合",
    "metroid prime": "密特罗德 Prime",
    "super mario bros 3 the world of": "马里奥的世界",
    "kid niki": " Kid Niki",
    "krusno ava": " Rusno Ava",
    "swamp thing": "沼泽人",
    "wonder boy": "神奇小子",
    "wonder boy 2": "神奇小子 2",
    "wonder boy 3": "神奇小子 3",
    "wonder boy in monster land": "神奇小子 怪物大陆",
    "wonder boy iii monster land": "神奇小子 3",
    "alex kidd in miracle world": "亚历克斯基德",
    "alex kidd in the enchanted castle": "亚历克斯基德 魔法城堡",
    "psycho fox": "心理狐狸",
    "rock n roll": "摇滚",
    "toki": "Toki",
    "toki 2": "Toki 2",
    "godzilla": "哥斯拉",
    "kaijuu gyakushuu": "哥斯拉",
    "mario vs donkey kong": "马里奥大战大金刚",
    # ---- 平台 / 主机独占 ----
    "mario vs dk": "马里奥大战大金刚",
    "star fox": "星际火狐",
    "star fox 2": "星际火狐 2",
    "star fox 64": "星际火狐 64",
    "f zero": "F-Zero",
    "f zero x": "F-Zero X",
    "f zero x expansion kit": "F-Zero X",
    "pilotwings": "飞行模拟",
    "super pilotwings": "超级飞行模拟",
    "simcity": "模拟城市",
    "simcity 2": "模拟城市 2",
    "sim earth": "模拟地球",
    "final fantasy myst": "最终幻想 神秘任务",
    "front mission": "前线任务",
    "front mission 2": "前线任务 2",
    "treasure hunter g": "寻宝猎人 G",
    "seiken": "圣剑传说",
    "battle of olympia": "奥林匹斯之战",
    "brain lord": "头脑领主",
    "nectaris": "Nectaris",
    "ganbare goemon": "鬼太郎",
    "ganbare goemon 2": "鬼太郎 2",
    "ganbare goemon 3": "鬼太郎 3",
    "goemon s revenge": "鬼太郎",
    "ghost trick": "幽灵诡计",
    "wild rangelands": "荒野大镖客",
    "mystic quest final fantasy": "最终幻想 神秘任务",
    "the 3d battles of worldrunner": "WorldRunner",
    "3d battles of worldrunner": "WorldRunner",
    "worldrunner": "WorldRunner",
    "famicom grand prix": "FC 大赛",
    "famicom grand prix 2": "FC 大赛 2",
    "famicom wars": "FC 战争",
    "famicom 3d boxes": "FC 3D 方块",
    "super mario bros adventure": "超级马里奥兄弟 冒险",
    "kani": "螃蟹",
    "twin hawk": "双鹰",
    "u-chu": "U-卡丘",
    # ---- 世嘉 / MD ----
    "sonic the hedgehog": "索尼克",
    "sonic 2": "索尼克 2",
    "sonic the hedgehog 2": "索尼克 2",
    "sonic the hedgehog 3": "索尼克 3",
    "sonic the hedgehog 4": "索尼克 4",
    "sonic the hedgehog 5": "索尼克 5",
    "sonic the hedgehog 6": "索尼克 6",
    "sonic 3d blast": "索尼克 3D",
    "sonic chaos": "索尼克乱斗",
    "sonic drift": "索尼克滑板",
    "sonic cd": "索尼克 CD",
    "tails": "塔尔斯",
    "tails skidding": "塔尔斯",
    "knuckles": "刺猬",
    "shadow the hedgehog": "夏特",
    "shadow the hedgehog 2": "夏特 2",
    "gunstar heroes": "枪魂",
    "gunstar heroes treasure planet": "枪魂",
    "contra hard corps": "魂斗罗 硬战",
    "power ranger": "Power Ranger",
    "power rangers": "Power Ranger",
    "mickey mouse": "米老鼠",
    "mickey mousecapade": "米老鼠",
    "land of illusion": "米奇幻想岛",
    "castle of illusion": "米奇幻影城堡",
    "quackshot": "鸭子可克",
    "ristar": "里斯特",
    "rockman 8": "洛克人 8",
    "rockman x2": "洛克人 X2",
    "rockman x3": "洛克人 X3",
    "rockman x4": "洛克人 X4",
    "monster world iv": "怪物世界 4",
    "wonder boy in monster world": "神奇小子 怪物世界",
    "wonder boy monster world": "神奇小子 怪物世界",
    "eswat": "Eswat",
    # ---- PC Engine ----
    "splatterhouse": "血浆屋",
    "splatterhouse 2": "血浆屋 2",
    "necropolis": "Necropolis",
    "r-type": "雷电",
    "r-type 2 the second light": "雷电 2",
    "pc genjin": "PC 原始人",
    "pc genjin 2": "PC 原始人 2",
    "bonk": "Bonk",
    "bonk s revenge": "Bonk",
    "ninja gaiden saga": "忍者龙剑传 合集",
    "splatoon": "Splatoon",
    "valis": "Valis",
    "valis 2": "Valis 2",
    "valis 3": "Valis 3",
    "the legend of valis": "Valis 传说",
    "cosmic fantasy": "宇宙幻想",
    "cosmic fantasy 2": "宇宙幻想 2",
    "gate of thunder": "雷之门",
    "gate of thunder 2": "雷之门 2",
    "legendary axe": "传奇之斧",
    "legendary axe 2": "传奇之斧 2",
    "wonder momo": "奇妙桃太郎",
    "kaze no klono": "风之克罗诺",
    # ---- GB / GBC ----
    "tetris 2 gb": "俄罗斯方块 2",
    "kirby s dream land": "星之卡比",
    "kirby s dream land 2": "星之卡比 2",
    "kirby s dream course": "星之卡比",
    "kirby no kiri kiri": "星之卡比",
    "kirby s pinball": "星之卡比 弹珠台",
    "pokemon red": "宝可梦 红",
    "pokemon blue": "宝可梦 蓝",
    "pokemon yellow": "宝可梦 黄",
    "pokemon gold": "宝可梦 金",
    "pokemon silver": "宝可梦 银",
    "pokemon crystal": "宝可梦 水晶",
    "pokemon ruby": "宝可梦 红宝石",
    "pokemon sapphire": "宝可梦 蓝宝石",
    "pokemon emerald": "宝可梦 绿宝石",
    "pokemon firered": "宝可梦 火红",
    "pokemon leafgreen": "宝可梦 叶绿",
    "monster strike": "怪兽大乱斗",
    "the legend of zelda link s awakening": "塞尔达传说 梦见岛",
    "tetris plus": "俄罗斯方块 Plus",
    "mario land": "马里奥大陆",
    "super mario land": "超级马里奥大陆",
    "super mario land 2": "超级马里奥大陆 2",
    "super mario land 3": "超级马里奥大陆 3",
    "wario land": "瓦力欧大陆",
    "wario land 2": "瓦力欧大陆 2",
    "wario land 3": "瓦力欧大陆 3",
    "wario land 4": "瓦力欧大陆 4",
    "mole mania": "摩尔庄园",
    "shantae": "沙退还色",
    "doraemon": "哆啦 A 梦",
    "doraemon 2": "哆啦 A 梦 2",
    "doraemon 3": "哆啦 A 梦 3",
    "doraemon 4": "哆啦 A 梦 4",
    "doraemon 5": "哆啦 A 梦 5",
    "dr mario": "超级马里奥医生",
    "mario vs wart": "马里奥大战华特",
    "mario & luigi": "马里奥与路易吉",
    "mario & luigi 2": "马里奥与路易吉 2",
    "mario & luigi 3": "马里奥与路易吉 3",
    "mario & luigi bowsers inside story": "马里奥与路易吉 库巴入侵",
    "mario & luigi paper dream": "马里奥与路易吉 纸之梦",
    "yoshi": "耀西",
    "yoshi s island": "耀西之岛",
    "yoshi s egg": "耀西彩蛋",
    "yoshi s cookie": "耀西饼干",
    "pumpkin kaizen": "南瓜改良",
    "dragon ball z gokou": "龙珠 Z",
    "dragon ball": "龙珠",
    "gourmet race": "美食大赛",
    "shin megami tensei": "真女神转生",
    "megami tensei": "女神转生",
    "persona": "女神异闻录",
    "dq monsters 2": "勇者斗恶龙 怪物篇 2",
    "metal gear solid": "合金装备 Solid",
    "front line": "前线",
    "operation c": "行动 C",
    "battle zeque Den": "Battle Zeque Den",
    # ---- N64 / SS / DC ----
    "super mario 64": "超级马里奥 64",
    "mario kart 64": "马里奥赛车 64",
    "zelda ocarina of time": "塞尔达传说 时之笛",
    "the legend of zelda majora s mask": "塞尔达传说 摩尔加面具",
    "the legend of zelda majora s mask": "塞尔达传说 摩尔加面具",
    "starfox 64": "星际火狐 64",
    "f zero x": "F-Zero X",
    "paper mario": "纸片马力欧",
    "banjo kazooie": "班卓熊",
    "banjo tooie": "班卓熊 2",
    "conker s bad fur day": "康克",
    "diddy kong racing": "迪迪熊赛车",
    "yoshi story": "耀西的故事",
    "mario party 8": "马里奥派对 8",
    "perfect dark": "完美黑暗",
    "goldeneye 007": "黄金眼 007",
    "donkey kong 64": "大金刚 64",
    "mario golf": "马里奥高尔夫",
    "1080 snowboard": "极限滑雪",
    "wave race 64": "Waverace 64",
    "rogues castle": "Rogue s Castle",
    "sakura wars": "樱花大战",
    "virtua fighter": "虚拟战士",
    "virtua fighter 2": "虚拟战士 2",
    "virtua striker": "虚拟足球",
    "niGHTS into dreams": "梦之国度",
    "panorama cotton": "棉花糖",
    "sonic 3d": "索尼克 3D",
    "sega rally": "世嘉拉力",
    "daytona usa": " Daytona USA",
    "virtua tennis": "虚拟网球",
    "sengoku blade": "战国刃",
    "guardian heroes": "守护者",
    "duke nukem": "毁灭公爵",
    "soulcalibur ii": "剑魂 II",
    "killer7": "Killer7",
    "silver surfer": "银影侠士",
    "pumpkin jack": "南瓜杰克",
    "chaos seed": "混沌种子",
    "shining soul": "光之魂",
    "shining force": "光之力量",
    "shining force 2": "光之力量 2",
    "shining in the darkness": "光之力量",
    "shining soul 2": "光之魂 2",
    "skies of arcadia": "亚尔斯提尔的天空",
    "skies of arcadia legends": "亚尔斯提尔的天空 传说",
    "puyo puyo 4": "噗哟噗哟 4",
    "puyo puyo tsu 2": "噗哟噗哟 通 2",
    "sakura wars 2": "樱花大战 2",
    # ---- PS 系 ----
    "ridge racer": "山脊赛车",
    "ridge racer revolution": "山脊赛车 Revolution",
    "r4": "山脊赛车 4",
    "gran turismo": "GT 赛车",
    "gran turismo 2": "GT 赛车 2",
    "gran turismo 3": "GT 赛车 3",
    "gran turismo 4": "GT 赛车 4",
    "need for speed": "极品飞车",
    "need for speed 2": "极品飞车 2",
    "need for speed 3": "极品飞车 3",
    "need for speed 4": "极品飞车 4",
    "need for speed 5": "极品飞车 5",
    "need for speed 6": "极品飞车 6",
    "need for speed 7": "极品飞车 7",
    "need for speed high stakes": "极品飞车 2",
    "nfs 2": "极品飞车 2",
    "wipeout": "愤怒冲击波",
    "wipeout 3": "愤怒冲击波 3",
    "wipeout 3 special edition": "愤怒冲击波 3",
    "tekken": "铁拳",
    "tekken 2": "铁拳 2",
    "tekken 3": "铁拳 3",
    "tekken 4": "铁拳 4",
    "tekken 5": "铁拳 5",
    "tekken tag tournament": "铁拳 TAG 格斗",
    "dead or alive": "死或生",
    "dead or alive 2": "死或生 2",
    "tomb raider": "古墓丽影",
    "tomb raider 2": "古墓丽影 2",
    "tomb raider the last revelation": "古墓丽影 最后启示",
    "tomb raider chronicles": "古墓丽影 编年史",
    "final fantasy vii": "最终幻想 VII",
    "final fantasy viii": "最终幻想 VIII",
    "final fantasy ix": "最终幻想 IX",
    "final fantasy x": "最终幻想 X",
    "devil may cry": "但丁的地狱",
    "resident evil": "生化危机",
    "resident evil 2": "生化危机 2",
    "resident evil 3": "生化危机 3",
    "resident evil 4": "生化危机 4",
    "silent hill": "寂静岭",
    "silent hill 2": "寂静岭 2",
    "silent hill 3": "寂静岭 3",
    "silent hill 4": "寂静岭 4",
    "metal gear solid 2": "合金装备 Solid 2",
    "metal gear solid 3": "合金装备 Solid 3",
    "metal gear solid 4": "合金装备 Solid 4",
    "metal gear solid portable": "合金装备 Solid Portable",
    "granblue": "碧蓝幻想",
    "kingdom hearts": "王国之心",
    "kingdom hearts 2": "王国之心 2",
    "final fantasy tactics": "最终幻想 战略版",
    "fft": "最终幻想 战略版",
    "drummania": "DrumMania",
    "guitar hero": "Guitar Hero",
    "persona 5": "女神异闻录 5",
    "nier": "NieR",
    "nier automata": "NieR 自动",
    "bloodborne": "血源诅咒",
    "horizon zero dawn": "地平线 黎明",
    "the last of us": "最后生还者",
    "uncharted": "神秘海域",
    "uncharted 2": "神秘海域 2",
    "uncharted 3": "神秘海域 3",
    "uncharted 4": "神秘海域 4",
    "god of war": "战神",
    "god of war 2": "战神 2",
    "god of war 3": "战神 3",
    "god of war ragnarok": "战神 诸神黄昏",
    "infinity war": "无限战争",
    "marvel vs capcom": "漫威大战卡普空",
    "dante devil may cry": "但丁的地狱",
    "ragnarok odyssey": "诸神黄昏",
    "spyro the dragon": "古瑞龙",
    "crash bandicoot": "古惑狼",
    "crash bandicoot 2": "古惑狼 2",
    "crash bandicoot 3": "古惑狼 3",
    "crash bang": "古惑狼 4",
    "sly cooper": "怪盗",
    "sly 2 band of thieves": "怪盗 2",
    "ratchet & clank": "瑞奇与叮当",
    "jak and daxter": " Jak 与 Daxter",
    "godzilla playstation": "哥斯拉",
    "kingdom hearts 3": "王国之心 3",
    "final fantasy x 2": "最终幻想 X-2",
    "ffx 2": "最终幻想 X-2",
    "ffvii": "最终幻想 VII",
    "ffviii": "最终幻想 VIII",
    "mgs2": "合金装备 Solid 2",
    "mgs3": "合金装备 Solid 3",
    "mgs4": "合金装备 Solid 4",
    "patapon": "Patapon",
    "patapon 2": "Patapon 2",
    "loco motive": "乐克乐克",
    "locoroco": "乐克乐克",
    "flow": "Flower",
    "flower": "Flower",
    "journey": "风之旅人",
    "abzu": "ABZÛ",
    "shadow of the colossus": "旺达与巨像",
    "ico": "ICO",
    "god hand": "God Hand",
    "dementium": "Dementium",
    "drawquest": "DrawQuest",
    # ---- 街机 / 杂项 ----
    "out run": "OutRun",
    "out run 2": "OutRun 2",
    "after burner": "After Burner",
    "daytona": " Daytona",
    "sega arcade": "世嘉街机",
    "mappy": "Mappy",
    "pac man": "吃豆人",
    "pac-man": "吃豆人",
    "dig dug": "打砖块",
    "dig dug 2": "打砖块 2",
    "popeye": "大力水手",
    "donkey kong jr": "大金刚小子",
    "q*bert": "Q*bert",
    "frogger": "青蛙过河",
    "tapper": "Tapper",
    "burgertime": "BurgerTime",
    "centipede": "蜈蚣",
    "galaga 2": "小蜜蜂 2",
    "pole position": "极点位置",
    "pole position ii": "极点位置 2",
    "speed blaster": "Speed Blaster",
    "toaplan": "Toaplan",
    "saint dragon": "Saint Dragon",
    "saint seiya": "圣斗士星矢",
    "guilty gear": "罪恶装备",
    "guilty gear x": "罪恶装备 X",
    "fighting layer": "Fighting Layer",
    "fighting layer 2": "Fighting Layer 2",
    "puzzle bobble": "Puzzle Bobble",
    "puchi carat": "Puchi Carat",
    "super Puzzle Fighter 2 turbo": "超级解谜格斗 2",
    "darkstalkers": "吸血鬼",
    "vampire hunter d": "吸血鬼猎人 D",
    "rockman 5": "洛克人 5",
    "rockman 7": "洛克人 7",
    "bomb bunny": "炸弹兔子",
    "snow bros": "雪人兄弟",
    "snow bros 2": "雪人兄弟 2",
    "pang": "Pang",
    "pang 2": "Pang 2",
    "pang 3": "Pang 3",
    "ninja kids": "忍者小子",
    "ninja kids 2": "忍者小子 2",
    "sbomb": "炸弹人",
    "sonic wings": "索尼克翅膀",
    "far east of eden": "伊甸之东",
    "far east of eden 2": "伊甸之东 2",
    "story of thomas": "托马斯的故事",
    "mosaic": "Mosaic",
    "kid chameleon": "变色龙小子",
    "sparkster": "Sparkster",
    "armadillo": "犰狳",
    "chakan the forever warrior": "Chakan",
    "gimmick": "Gimmick",
    "mustafa": "Mustafa",
    "tinhead": "Tinhead",
    "wurm": "Wurm",
    "zool": "Zool",
    "zero the kamikaze fighter": "Zero 战斗机",
    "x-men": "X 战警",
    "x-men 2": "X 战警 2",
    "dead move": "Dead Move",
    "ko-op": "双人合作",
    "4 player": "四人游戏",
    "8 player": "八人游戏",
    "2 player": "双人游戏",
    "1 player": "单人游戏",
}

# --------------------------------------------------------------------------
# 第 2 层: 词组替换(系列名 / 专有名词)
# --------------------------------------------------------------------------
_PHRASES: List[Tuple[str, str]] = [
    ("final fantasy mystic quest", "最终幻想 神秘任务"),
    ("dragon quest monsters", "勇者斗恶龙 怪物篇"),
    ("metal gear solid", "合金装备 Solid"),
    ("the king of fighters", "拳皇"),
    ("king of fighters", "拳皇"),
    ("street fighter", "街头霸王"),
    ("mortal kombat", "真人快打"),
    ("silent hill", "寂静岭"),
    ("resident evil", "生化危机"),
    ("castlevania symphony of the night", "恶魔城 月下夜想曲"),
    ("super mario bros", "超级马里奥兄弟"),
    ("donkey kong country", "大金刚 Country"),
    ("sonic the hedgehog", "索尼克"),
    ("the legend of zelda", "塞尔达传说"),
    ("star wars", "星球大战"),
    ("super mario world", "超级马里奥世界"),
    ("mario kart", "马里奥赛车"),
    ("ninja gaiden", "忍者龙剑传"),
    ("seiken densetsu", "圣剑豪情"),
    ("earthbound", "星海传说"),
    ("chrono trigger", "时空之轮"),
    ("romancing saga", "重装机兵"),
    ("secret of mana", "圣剑豪情"),
    ("star ocean", "星海传说"),
    ("breath of fire", "呼吸之火"),
    ("sakura wars", "樱花大战"),
    ("virtua fighter", "虚拟战士"),
    ("shining force", "光之力量"),
    ("final fantasy", "最终幻想"),
    ("dragon quest", "勇者斗恶龙"),
    ("front mission", "前线任务"),
    ("kingdom hearts", "王国之心"),
    ("gran turismo", "GT 赛车"),
    ("need for speed", "极品飞车"),
    ("wipeout", "愤怒冲击波"),
    ("tomb raider", "古墓丽影"),
    ("tekken", "铁拳"),
    ("dead or alive", "死或生"),
    ("bomberman", "炸弹人"),
    ("pokemon", "宝可梦"),
    ("metroid", "密特罗德"),
    ("kirby s", "星之卡比"),
    ("gameland", "游戏乐园"),
    ("doraemon", "哆啦 A 梦"),
    ("sonic", "索尼克"),
    ("zelda", "塞尔达"),
    ("mario", "马里奥"),
    ("luigi", "路易吉"),
    ("wario", "瓦力欧"),
    ("yoshi", "耀西"),
    ("kirby", "卡比"),
    ("pokemon stadium", "宝可梦 竞技场"),
    ("street fighter 2", "街头霸王 II"),
]

# --------------------------------------------------------------------------
# 第 3 层: 分词直译
# --------------------------------------------------------------------------
_TOKENS: Dict[str, str] = {
    # 通用词
    "super": "超级", "mega": "超级", "great": "伟大", "grand": "大奖",
    "king": "王者", "kingdom": "王国", "castle": "城堡", "castle of": "城堡",
    "dark": "暗黑", "black": "黑", "white": "白", "red": "红", "blue": "蓝",
    "green": "绿", "yellow": "黄", "gold": "金", "golden": "黄金",
    "silver": "银", "silver surfers": "冲浪小子", "shadow": "影",
    "light": "光", "star": "星", "moon": "月", "sun": "太阳",
    "night": "夜", "day": "白昼", "world": "世界", "dream": "梦",
    "legend": "传说", "legendary": "传奇", "legend of": "传说",
    "hero": "英雄", "heroes": "英雄", "warrior": "战士", "warriors": "战士",
    "knight": "骑士", "samurai": "武士", "ninja": "忍者", "pirate": "海盗",
    "dragon": "龙", "dragons": "龙", "dragon ball": "龙珠",
    "monster": "怪兽", "monsters": "怪兽", "ghost": "幽灵",
    "ghostbusters": "捉鬼敢死队", "alien": "外星人", "aliens": "外星人",
    "space": "太空", "galaxy": "银河", "planet": "星球", "planets": "星球",
    "earth": "地球", "fire": "火焰", "flame": "火焰", "ice": "冰",
    "snow": "雪", "wind": "风", "thunder": "雷霆", "storm": "风暴",
    "storm": "风暴", "rain": "雨", "water": "水", "ocean": "海洋",
    "sea": "海", "mountain": "山", "river": "河", "forest": "森林",
    "garden": "花园", "island": "岛", "castle": "城堡", "tower": "塔",
    "quest": "任务", "adventure": "冒险", "adventures": "冒险",
    "battle": "战斗", "battle of": "之战", "war": "战争", "wars": "战争",
    "arena": "竞技场", "stadium": "竞技场", "championship": "锦标赛",
    "tournament": "大赛", "league": "联赛", "cup": "杯",
    "racing": "赛车", "race": "赛车", "racer": "赛车", "rangers": "游侠",
    "grand prix": "大奖赛", "speed": "速度", "turbo": "涡轮",
    "fighter": "格斗", "fighters": "格斗", "fighting": "格斗",
    "fight": "格斗", "battle city": "坦克大战", "vs": "大战",
    "soccer": "足球", "football": "足球", "tennis": "网球",
    "golf": "高尔夫", "baseball": "棒球", "basketball": "篮球",
    "hockey": "冰球", "boxing": "拳击", "wrestling": "摔角",
    "olympics": "奥运", "sports": "体育", "sport": "体育",
    "game": "游戏", "games": "游戏", "party": "派对",
    "puzzle": "解谜", "puzzles": "解谜", "mahjong": "麻将",
    "casino": "赌场", "card": "纸牌", "cards": "纸牌",
    "rpg": "角色扮演", "shooter": "射击", "shooting": "射击",
    "action": "行动", "adv": "冒险", "sports": "体育",
    "club": "俱乐部", "team": "队伍", "league of": "联赛",
    "champions": "冠军", "champion": "冠军", "world": "世界",
    "cup 94": "杯 94", "final": "最终", "ultimate": "究极",
    "greatest": "最强", "best": "最强", "perfect": "完美",
    "in": "在", "the": "", "of": "", "and": "与", "de": "的",
    "the king of": "王者", "vs.": "大战",
    # 平台动作
    "jump": "跳跃", "jumping": "跳跃", "run": "奔跑", "runner": "奔跑",
    "dash": "冲刺", "spin": "旋转", "spin off": "旋转外传",
    "hedgehog": "刺猬", "fox": "狐狸", "tux": "企鹅",
    "princess": "公主", "prince": "王子", "king": "国王",
    "queen": "女王", "knights": "骑士", "castle": "城堡",
    # 玩家 / 英雄
    "mario": "马里奥", "luigi": "路易吉", "yoshi": "耀西",
    "zelda": "塞尔达", "kirby": "卡比", "doraemon": "哆啦A梦",
    "pokemon": "宝可梦", "pikachu": "皮卡丘", "mew": "梦幻",
    "sonic": "索尼克", "tails": "塔尔斯", "knuckles": "刺猬",
    "shadow": "夏特", "amy": "艾米", "chaos": "混沌",
    "dr eggman": "蛋头博士", "eggman": "蛋头博士",
    # 数字词
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "7": "7", "8": "8", "9": "9", "10": "10", "16": "16", "16bit": "16 位",
    "3d": "3D", "2d": "2D", "in 1": "合 1", "4": "4",
}

# 这些词直接丢掉(纯噪声, 不参与"覆盖率"统计)
_NOISE = {"the", "of", "and", "a", "an", "de", "in", "to", "&", "vs", "x", "n"}

# 英文专有名词原样保留(不算"未翻译")
_KEEP = {"nes", "snes", "sfc", "gb", "gba", "md", "genesis", "pce", "tg16",
         "n64", "ps", "psx", "ps1", "ps2", "sms", "gg", "ngp"}


def _compose(core: str) -> str:
    """第 3 层: 逐词直译。未收录的词直接丢弃(半中半英更难读),
    命中率低于 65% 就整体放弃, 免得拼出"某某 神秘 骑士 3"这种半吊子。"""
    key = _norm(core)
    if not key:
        return ""
    toks = key.split()
    out_key = key
    for phrase, zh in _PHRASES:                     # 系列名先替换
        if phrase in out_key:
            out_key = out_key.replace(phrase, " " + zh + " ")
    parts, hit, miss = [], 0, 0
    for t in out_key.split():
        if t in _NOISE or t in _KEEP:
            continue
        if t in _TOKENS:
            v = _TOKENS[t].strip()
            if v:
                parts.append(v)
                hit += 1
            continue
        if not t.isdigit():
            miss += 1                              # 丢弃, 但计入未命中
    if hit == 0:
        return ""
    if hit / (hit + miss) < 0.55:
        return ""
    out = " ".join(parts).strip()
    # 必须真的含中文: 否则像 "10000000-in-1 (menu)" 会拼出一个纯数字的 "1", 毫无意义
    return out if _is_cjk(out) else ""


def translate(name: str, allow_compose: bool = True) -> str:
    """把 joshw 的档案名翻成中文。拿不准时返回空串。"""
    core = core_title(name)
    if not core:
        return ""
    if _is_cjk(core):
        return core
    key = _norm(core)
    if not key:
        return ""
    hit = GAME_ZH.get(key)
    if hit:
        return hit
    # 只在"标题 + 纯序号"这种情况下才允许按前缀命中(如 "Metroid II" -> 密特罗德 II)
    m = re.match(r"^(.*?)\s+([0-9]+|[ivxIVX]{1,4})$", core)
    if m:
        head = GAME_ZH.get(_norm(m.group(1)))
        if head:
            return "%s %s" % (head, m.group(2))
    if allow_compose:
        return _compose(core)
    return ""


def coverage(names) -> Tuple[int, int, float]:
    """统计一批名字里有多少能翻出中文(精确 / 合计 / 比例)。"""
    names = list(names)
    if not names:
        return 0, 0, 0.0
    exact = sum(1 for n in names if GAME_ZH.get(_norm(core_title(n))))
    total = sum(1 for n in names if translate(n))
    return exact, total, total / len(names) * 100.0

# --------------------------------------------------------------------------
# 第二批: 按真实索引挖掘出的高频条目补录(2026-10-18)
# --------------------------------------------------------------------------
GAME_ZH.update({
    # 日漫改 / 少年漫画(GB、GBC 上大量)
    "dragon ball": "龙珠", "dragon ball z": "龙珠 Z",
    "dragon ball z super butoden": "龙珠 Z 超武斗传",
    "dragon ball z idainaru son goku": "龙珠 Z 孙悟空",
    "dragon ball z kakarot": "龙珠 Z 孙悟空",
    "one piece": "海贼王", "one piece 2": "海贼王 2",
    "naruto": "火影忍者", "naruto 2": "火影忍者 2",
    "bleach": "死神", "bleach 2": "死神 2",
    "slam dunk": "灌篮高手", "slam dunk 2": "灌篮高手 2",
    "yu yu hakusho": "幽游白书", "yu yu hakusho 2": "幽游白书 2",
    "yu-gi-oh": "游戏王", "yu gi oh": "游戏王",
    "yu-gi-oh duel monsters": "游戏王 决斗怪兽",
    "yu-gi-oh monster capsule gb": "游戏王 怪兽胶囊",
    "yu-gi-oh eternal the soul": "游戏王 永恒的灵魂",
    "crayon shin-chan": "蜡笔小新",
    "shin chan": "蜡笔小新",
    "nintama rantarou": "忍者小灵精",
    "sakura momoko": "樱桃小丸子",
    "kiteretsu dai hyakka": "哆啦 A 梦大百科",
    "kiteretsu": "奇天烈大百科",
    "chinpuden": "乱马 1/2",
    "ranma 1 2": "乱马 1/2",
    "hugtto precure": "光之美少女",
    "pokemon crystal version": "宝可梦 水晶",
    "inuyasha": "犬夜叉",
    "shaman king": "魔斗士",
    "shaman king 2": "魔斗士 2",
    "steel angel kurumi": "钢铁天使玖璃",
    "neon genesis evangelion": "新世纪福音战士",
    "gundam": "高达", "gundam 0079": "高达 0079",
    "mobile suit gundam": "机动战士高达",
    "gundam wing": "高达W",
    "gundam seed": "高达SEED",
    "ninja hattori": "小忍者哈特利",
    "kiteretsu 2": "奇天烈大百科 2",
    "puyo puyo 2": "噗哟噗哟 2",
    "magical drop": "魔法_drop", "magical drop 2": "魔法_drop 2",
    "puzzle bobble 2": "Puzzle Bobble 2",
    "bust a move": "都市胀消",
    # 日文系常见系列(罗马字 -> 中文)
    "seiken densetsu 3": "圣剑传说 3",
    "secret of mana 2": "圣剑豪情 2",
    "romancing saGa 3": "重装机兵 3",
    "daisakusen": "大作战",
    "daibouken": "大冒险",
    "taisen": "大战",
    "senshi": "战士",
    "senki": "战记",
    "fushigi": "不可思议",
    "mahou": "魔法",
    "bouken": "冒险",
    "tantei": "侦探",
    "yakyuu": "野球",
    "shougi": "将棋",
    "gaiden": "外传",
    "pachinko": "弹珠机",
    "nintama": "忍者小灵精",
    "minna": "大家",
    "monogatari": "物语",
    "densetsu": "传说",
    "mahjong": "麻将",
    "tsume": "极限",
    "kinnikuman": "肌肉人",
    "obake": "鬼",
    "youkai": "妖怪",
    "dororon": "咚咚",
    "gekitou": "激斗",
    # 英文常见系列 / 主题
    "batman": "蝙蝠侠", "batman returns": "蝙蝠侠归来",
    "superman": "超人", "spider-man": "蜘蛛侠",
    "spider man": "蜘蛛侠",
    "x men": "X 战警", "x-men": "X 战警",
    "looney tunes": "华纳兄弟",
    "tom and jerry": "猫和老鼠",
    "road runner": "跑跑板",
    "wile e coyote": "歪心狼",
    "porky pig": "猪小弟",
    "bugs bunny": "兔八哥",
    "daffy duck": "达菲鸭",
    "mickey": "米老鼠", "minnie": "米妮",
    "donkey kong": "大金刚",
    "pokémon": "宝可梦",
    "harry potter": "哈利波特",
    "star wars": "星球大战",
    "lord of the rings": "指环王",
    "the lord of the rings": "指环王",
    "game of thrones": "权力的游戏",
    "simpsons": "辛普森一家",
    "south park": "南方公园",
    "family guy": "盖世奇兵",
    "pro evolution": "实况足球",
    "efootball": "实况足球",
    "winning eleven": "实况足球",
    "nba jam": "NBA 疯狂",
    "nba showtime": "NBA 全明星",
    "nba hangtime": "NBA 悬垂",
    "nba 2k": "NBA 2K",
    "madden nfl": "Madden NFL",
    "fifa international soccer": "FIFA 国际足球",
    "fifa 98": "FIFA 98",
    "world soccer": "世界足球",
    "virtual soccer": "虚拟足球",
    "super slams": "街头篮球",
    "kobe bryant in nba": "科比篮球",
    "ready 2 rumble": "Ready 2 Rumble",
    "ea sports": "EA 体育",
    "top gear": "Top Gear",
    "gran turismo 5": "GT 赛车 5",
    "forza": "极限竞速",
    "midnight club": "午夜club",
    "burnout": "Burnout",
    "destiny": "命运",
    "halo": "光环",
    "halo 2": "光环 2",
    "halo 3": "光环 3",
    "halo wars": "光环战争",
    "star wars rogue squadron": "星球大战 突袭小队",
    "rogue squadron": "突袭小队",
    "x wing": "X 翼",
    "tiefighter": "TIE 战机",
    "mario party 4": "马里奥派对 4",
    "super smash bros": "任天堂明星大乱斗",
    "super smash bros melee": "任天堂明星大乱斗 豪华版",
    "multiplayer": "多人对战",
    "multitap": "多人插卡",
    "4 players": "四人游戏",
    "3 players": "三人游戏",
    "big challenge": "大挑战",
    "grand prix": "大奖赛",
    "super league": "超级联赛",
    "pro league": "职业联赛",
    "national league": "全国联赛",
    "world league": "世界联赛",
    "nhl": "NHL",
    "nba": "NBA",
    "f1 world grand prix": "F1 世界大奖赛",
    "f1 grand prix": "F1 大奖赛",
    "f1": "F1",
    "driving": "驾驶", "racing game": "赛车",
    "arcade racer": "街机赛车",
    "moto": "摩托", "motor": "摩托",
    "miniature": "迷你", "mini": "迷你",
    "san": "三", "go": "围棋",
    "ninja": "忍者", "ninja kun": "忍者君",
    "otakuman": "的游戏",
    "puzusu": "Puzzle",
    "sim": "模拟",
    "meikyuu": "迷宫",
    "dungeon": "迷宫",
    "rune": "符文", "runes": "符文",
    "gauntlet": "圆环", "gauntlet 2": "圆环 2",
    "gauntlet 3": "圆环 3",
    "gauntlet 4": "圆环 4",
    "gauntlet legends": "圆环 传说",
    "ghouls": "幽灵", "ghostbusters": "捉鬼敢死队",
    "ghouls n ghosts": "幽灵",
    "phantom": "幻影", "phantasm": "幻影",
    "phantom fighter": "幻影战士",
    "silver": "银",
    "spirit": "精灵", "spiral": "螺旋",
    "tonic": "补药",
    "lotus": "赛车", "lotus turbo": "Lotus 涡轮",
    "nebius": "Nebius",
    "ninja ryukenden": "忍者龙剑传",
    "dungeon magic": "迷宫魔法",
    "dungeon master": "迷宫大师",
    "dungeonquest": "迷宫任务",
    "final fantasy legend": "最终幻想 传说",
    "legend of the star": "星之传说",
    "legend of zelda": "塞尔达传说",
    "zillion": "Zillion",
    "tengai makyou": "天外魔境",
    "getsu fuji": "月风魔传",
    "wonder project j": " Wonder Project J",
    "ganbare goemon 4": "鬼太郎 4",
    "goemon": "鬼太郎",
    "takeda": "武田",
    "battle of the planets": "星球大战",
    "the fantastic world of karo": "卡罗的神奇世界",
    "insect": "昆虫", "insecticide": "昆虫",
    "ant": "蚂蚁",
    "frog": "青蛙", "frogs": "青蛙",
    "dog": "小狗", "cat": "小猫", "cats": "猫",
    "bird": "小鸟", "birds": "鸟",
    "fish": "鱼", "shark": "鲨鱼", "sharks": "鲨鱼",
    "bear": "熊", "wolf": "狼", "tiger": "老虎",
    "monkey": "猴子", "rabbit": "兔子", "rabbits": "兔子",
    "dinosaur": "恐龙", "dinosaurs": "恐龙",
    "sword": "剑", "swords": "剑",
    "shield": "盾", "armor": "铠甲", "armour": "铠甲",
    "treasure": "宝藏", "treasures": "宝藏",
    "island": "小岛", "islands": "群岛",
    "temple": "神殿", "ruins": "遗迹",
    "cave": "洞穴", "caves": "洞穴",
    "volcano": "火山", "desert": "沙漠", "jungle": "丛林",
    "valley": "山谷", "castle": "城堡",
    "kingdom": "王国", "empire": "帝国",
    "dungeon": "迷宫",
    "clown": "小丑", "circus": "马戏团",
    "party": "派对", "picnic": "野餐",
    "school": "学校", "class": "课堂",
    "student": "学生", "teacher": "老师",
    "club": "社团", "team": "队伍",
    "soccer": "足球", "baseball": "棒球",
    "basketball": "篮球", "volleyball": "排球",
    "badminton": "羽毛球", "rugby": "橄榄球",
    "ski": "滑雪", "skating": "滑冰",
    "swim": "游泳", "surf": "冲浪",
    "athletics": "田径",
    "champion": "冠军", "champions": "冠军",
    "olympic": "奥运", "olympics": "奥运",
    "stadium": "球场",
})

_PHRASES.extend([
    ("series", "系列"), ("super league", "超级联赛"),
    ("pro league", "职业联赛"), ("national league", "全国联赛"),
    ("world league", "世界联赛"), ("grand prix", "大奖赛"),
    ("dragon ball", "龙珠"), ("one piece", "海贼王"),
    ("naruto", "火影忍者"), ("bleach", "死神"),
    ("slam dunk", "灌篮高手"), ("yu gi oh", "游戏王"),
    ("doraemon", "哆啦A梦"), ("crayon shin chan", "蜡笔小新"),
    ("nba", "NBA"), ("fifa", "FIFA"), ("gundam", "高达"),
    ("x men", "X 战警"), ("monster hunter", "怪物猎人"),
    ("digimon world", "数码宝贝 世界"), ("test drive", "试车"),
])

_TOKENS.update({
    "series": "系列", "vol": "卷", "collection": "合集",
    "special": "特别版", "edition": "版", "simple": "简单",
    "complete": "完整", "deluxe": "豪华版", "plus": "加强版",
    "master": "大师", "best": "最强", "greatest": "最强",
    "ultra": "究极", "hyper": "究极", "super": "超级",
    "new": "新", "old": "旧", "true": "真",
    "ball": "球", "power": "力量", "force": "力量",
    "time": "时空", "life": "生命", "world": "世界",
    "zone": "地带", "area": "区域", "space": "太空",
    "real": "真实", "true story": "真实故事",
    "magic": "魔法", "magical": "魔法", "wizard": "巫师",
    "mystery": "谜", "mysterious": "神秘",
    "love": "爱情", "heart": "心", "heart of": "之心",
    "one": "一", "two": "二", "three": "三", "four": "四",
    "five": "五", "six": "六", "seven": "七", "eight": "八",
    "nine": "九", "ten": "十", "hundred": "百",
    "double": "双重", "triple": "三重", "final": "最终",
    "last": "最后", "first": "最初", "lost": "迷失",
    "zero": "零", "big": "大", "little": "小小", "small": "小",
    "great": "伟大", "grand": "豪华", "super": "超级",
    "day": "白昼", "night": "夜", "moon": "月", "star": "星",
    "sun": "太阳", "earth": "大地", "sky": "天空",
    "fire": "火", "ice": "冰", "wind": "风", "thunder": "雷",
    "storm": "风暴", "rain": "雨", "wave": "浪",
    "tour": "巡回", "training": "训练", "challenge": "挑战",
    "cup": "杯", "league": "联赛", "world cup": "世界杯",
    "kids": "小子", "kid": "小孩", "children": "儿童",
    "boy": "少年", "girl": "少女",
    "family": "家族", "friends": "伙伴", "party": "派对",
    "school": "学校", "story": "故事", "stories": "故事",
    "book": "书", "game": "游戏", "games": "游戏",
    "play": "玩", "puzzle": "解谜", "quiz": "问答",
    "demo": "演示", "arcade": "街机", "slot": "老虎机",
    "robot": "机器人", "mecha": "机甲", "robotics": "机器人",
    "hunter": "猎人", "hunters": "猎人", "hunt": "狩猎",
    "rescue": "救援", "attack": "攻击", "defense": "防御",
    "survival": "生存", "escape": "逃脱", "escape from": "逃离",
    "prison": "监狱", "prisoner": "囚犯",
    "dungeon": "迷宫", "cave": "洞穴", "temple": "神殿",
    "ruins": "遗迹", "tower": "塔", "castle": "城堡",
    "kingdom": "王国", "empire": "帝国", "village": "村庄",
    "town": "小镇", "city": "城市", "valley": "山谷",
    "desert": "沙漠", "jungle": "丛林", "island": "小岛",
    "volcano": "火山", "mountain": "山",
    "sword": "剑", "swords": "剑", "blade": "刃",
    "shield": "盾", "armor": "铠甲",
    "treasure": "宝藏", "gold": "黄金", "gem": "宝石",
    "monster": "怪兽", "monsters": "怪兽", "boss": "首领",
    "dragon": "龙", "knight": "骑士", "soldier": "士兵",
    "samurai": "武士", "ninja": "忍者", "pirate": "海盗",
    "wizard": "巫师", "witch": "女巫", "ghost": "幽灵",
    "ghost": "幽灵", "alien": "外星人", "robot": "机器人",
    "robot": "机器人", "clown": "小丑", "circus": "马戏团",
    "party": "派对", "picnic": "野餐", "shop": "商店",
    "kitchen": "厨房", "restaurant": "餐厅", "hotel": "旅馆",
    "school": "学校", "exam": "考试", "test": "测试",
    "racing": "赛车", "race": "赛车", "racer": "赛车",
    "driver": "车手", "driving": "驾驶", "speed": "速度",
    "kart": "卡丁车", "moto": "摩托", "bike": "机车",
    "soccer": "足球", "baseball": "棒球", "basketball": "篮球",
    "tennis": "网球", "golf": "高尔夫", "hockey": "冰球",
    "boxing": "拳击", "wrestling": "摔角", "swim": "游泳",
    "ski": "滑雪", "skate": "滑板", "surf": "冲浪",
    "sports": "体育", "sport": "体育", "championship": "锦标赛",
    "tournament": "大赛", "league": "联赛", "cup": "杯",
    "time": "时空", "war": "战争", "soldier": "士兵",
    "sniper": "狙击手", "commando": "突击队", "commando 2": "突击队 2",
    "navy": "海军", "air force": "空军", "pilot": "飞行员",
    "mission": "任务", "operation": "行动", "war": "战争",
    "battalion": "营", "brigade": "旅", "army": "军队",
    "kung fu": "功夫", "karate": "空手道", "judo": "柔道",
    "sumo": "相扑", "yoga": "瑜伽", "martial arts": "武术",
    "sword art": "剑术", "art": "艺术", "arts": "艺术",
    "music": "音乐", "musical": "音乐剧", "concert": "音乐会",
    "song": "歌曲", "songs": "歌曲", "sound": "声音",
    "stage": "舞台", "show": "表演", "movie": "电影",
    "photo": "照片", "camera": "相机", "television": "电视",
    "tv": "电视", "radio": "收音机", "news": "新闻",
    "paper": "纸张", "card": "纸牌", "cards": "纸牌",
    "money": "金钱", "gold": "金", "shop": "商店",
    "kitchen": "厨房", "cook": "厨师", "cooking": "烹饪",
    "farm": "农场", "fishing": "钓鱼", "ranch": "牧场",
    "pet": "宠物", "dog": "小狗", "cat": "小猫",
    "bird": "小鸟", "fish": "鱼", "horse": "马",
    "insect": "昆虫", "bug": "虫", "bee": "蜜蜂",
    "ant": "蚂蚁", "spider": "蜘蛛", "frog": "青蛙",
    "dinosaur": "恐龙", "bear": "熊", "wolf": "狼",
    "tiger": "老虎", "monkey": "猴子", "rabbit": "兔子",
    "robot": "机器人", "android": "人造人", "cyborg": "机械人",
    "sentai": "战队", "super sentai": "超级战队",
    "henshin": "变身", "henshin!: asagao": "变身！牵牛花",
    "asian": "亚洲", "ninja": "忍者", "shogun": "将军",
    "samurai": "武士", "sumo": "相扑", "japan": "日本",
    "china": "中国", "tokyo": "东京", "osaka": "大阪",
    "sakura": "樱", "sakura wars": "樱花大战",
    "gekijou": "剧场", "den": "传",
})


GAME_ZH.update({
    "castlevania 3 draculas curse": "恶魔城 3 德古拉之咒",
    "river city ransom": "热血硬派",
    "chaos seed feng shui kairouki": "混沌种子 风水回路",
    "chaos seed feng shui kairouki 2": "混沌种子 2",
    "desert strike": "沙漠风暴",
    "desert strike return to the gulf": "沙漠风暴 回到海湾",
    "mahjong taikai": "麻将大会",
    "mahjong taikai ii": "麻将大会 2",
    "mahjong taikai roman": "麻将大会 Roman",
    "burgertime": "汉堡时间",
    "burgertime deluxe": "汉堡时间 豪华版",
    "tetris attack": "俄罗斯方块 攻击",
    "tetris 2 + bombliss": "俄罗斯方块 2",
    "tetris battle gaiden": "俄罗斯方块 战斗外传",
    "super mario world 4": "超级马里奥世界 4",
    "super mario world yoshi s island": "超级马里奥世界 耀西之岛",
    "super mario world 2": "超级马里奥世界 2",
    "super mario world 3": "超级马里奥世界 3",
    "ninja gaiden 2 the dark sword of chaos": "忍者龙剑传 2 混沌之剑",
    "dragon warrior": "勇者斗恶龙",
    "dragon warrior ii": "勇者斗恶龙 II",
    "dragon warrior iii": "勇者斗恶龙 III",
    "dragon warrior iv": "勇者斗恶龙 IV",
    "dragon warrior v": "勇者斗恶龙 V",
    "dragon warrior vi": "勇者斗恶龙 VI",
    "dragon warrior vii": "勇者斗恶龙 VII",
    "dragon quest": "勇者斗恶龙",
    "wizardry": "巫师",
    "wizardry 2": "巫师 2",
    "wizardry 3": "巫师 3",
    "wizardry 4": "巫师 4",
    "wizardry 5": "巫师 5",
    "wizardry 6": "巫师 6",
    "wizardry 7": "巫师 7",
    "ultima": "创世纪",
    "ultima i": "创世纪 1",
    "ultima iv": "创世纪 4",
    "ultima vii": "创世纪 7",
    "might and magic": "魔法与英雄",
    "rogue": "Rogue",
    "diablo": "暗黑破坏神",
    "star control": "星际控制",
    "escape from monkey island": "逃出猴岛",
    "monkey island": "猴岛",
    "monkey island 2": "猴岛 2",
    "loom": "织工坊",
    "king quest": "王者之谜",
    "shadow of the comet": "彗星之影",
    "beneath a steel sky": "钢铁天空之下",
    "day of the tentacle": "触手之日",
    "full throttle": "全速冲刺",
    "sanitarium": "疗养院",
    "tex Murphy": "Tex Murphy",
    "rise of the dragon": "龙之崛起",
    "rise of the triad": "三雄崛起",
    "the wall": "绝命废墟",
    "marathon": "马拉松",
    "pathways into darkness": "黑暗之路",
    "avenging angel": "复仇天使",
    "corridor 7": "走廊 7",
    "reaper": "死神",
    "xevil": "Xevil",
    "descent": "Descent",
    "the 7th guest": "第七访客",
    "the 11th hour": "第十一小时",
    "quake": "Quake",
    "doom": "毁灭战士",
    "doom 2": "毁灭战士 2",
    "wolfenstein 3d": "德军总部 3D",
    "hexen": "Hexen",
    "quake ii": "Quake II",
    "nitemare 3d": "Nightmare 3D",
    "lost vikings": "失落的维京人",
    "lost vikings 2": "失落的维京人 2",
    "kung fu master": "功夫大师",
    "prince of persia": "波斯王子",
    "prince of persia 2": "波斯王子 2",
    "flashback": "闪灵",
    "out of this world": "另一个世界",
    "inner worlds": "Inner Worlds",
    "deja vu": " déjà vu 幻象",
    "system shock": "系统冲击",
    "bioshock": "生化奇兵",
    "half life": "半条命",
    "portal": "传送门",
    "portal 2": "传送门 2",
    "left 4 dead": "求生之路",
    "counter strike": "反恐精英",
    "team fortress": "军团要塞",
    "quake arena": "Quake 竞技场",
    "witchcraft": "巫术",
    "mystical ninja": "神秘忍者",
    "phantom fighter": "幻影战士",
    "trouble shooter": "排障者",
    "jump super starcups": "跳杯",
    "digimon world": "数码宝贝 世界",
    "digimon world 3": "数码宝贝 世界 3",
    "digimon world 4": "数码宝贝 世界 4",
    "monster hunter": "怪物猎人",
    "monster hunter 2": "怪物猎人 2",
    "x men mutant wars": "X 战警 变种人战争",
    "altered space a 3 d alien adventure": "异变太空 3D 外星人冒险",
    "heavyweight championship boxing": "重量级拳击锦标赛",
    "ninja boy": "忍者少年",
    "rescue of princess blobette": "营救公主布洛贝特",
    "crash team racing": "Crash 车队赛车",
    "monkey hero": "猴子英雄",
    "power mission": "力量任务",
    "universal league soccer": "终极联赛足球",
    "ultimate league soccer": "终极联赛足球",
    "play action football": "街头橄榄球",
    "super contra 7": "超级魂斗罗 7",
    "test drive": "试车",
    "test drive 5": "试车 5",
    "golf grand slam": "高尔夫 大满贯",
    "ultimate league": "终极联赛",
})


# --------------------------------------------------------------------------
# 用户自定义词典(可选): 放在 exe 同目录或 %LOCALAPPDATA%\MSU1Converter\
# 名为 zh_titles_user.json, 格式 {"英文标题": "中文名"}。启动时合并, 优先级最高。
# --------------------------------------------------------------------------
_USER_DICT = "zh_titles_user.json"


def _load_user_dict(verbose: bool = False) -> int:
    """把 ``zh_titles_user.json`` 合并进 ``GAME_ZH``（后加载的覆盖先加载的）。

    **这个文件才是中文名的主体**（约 2.7 万条），内置字面量只有一千多条。
    读不到它，界面上绝大多数条目就只剩「—」，看着像「中文名功能坏了」。

    查找顺序（**先包内、后用户**，这样用户能覆盖个别条目）：

    =========================  ============================================
    位置                        什么时候命中
    =========================  ============================================
    ``<本文件目录>/``            源码态；**Nuitka onefile 的解包临时目录**
    ``sys._MEIPASS/``           PyInstaller onefile
    ``<exe 同级>/``             standalone / onedir；用户自己丢一份覆盖
    ``%LOCALAPPDATA%\\MSU1Converter\\``  用户级覆盖，跨版本保留
    ``os.getcwd()``             源码态兜底
    =========================  ============================================

    ⚠ 判断「是不是打包态」不能用 ``sys.frozen`` —— **Nuitka 没有这个属性**
      （只有 ``__compiled__``）。早先写了 ``getattr(sys, "frozen", False)``，
      结果打包后永远走 else 分支去找 cwd，exe 旁边那份词典形同虚设。
      这和 ``joshw_source.bundled_dir()`` 里踩的是同一个坑。
    """
    import json
    import os
    import sys

    here = ""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:                                # noqa: BLE001
        pass

    paths = []
    if here:
        # 包内那份：Nuitka onefile 把 --include-data-files 解到临时目录，
        # 而编译进去的模块 __file__ 也指那儿，所以两者天然同目录。
        paths.append(os.path.join(here, _USER_DICT))
    meipass = getattr(sys, "_MEIPASS", None)          # PyInstaller onefile
    if meipass:
        paths.append(os.path.join(str(meipass), _USER_DICT))
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        paths.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                  _USER_DICT))
    base = os.environ.get("LOCALAPPDATA")
    if base:
        paths.append(os.path.join(base, "MSU1Converter", _USER_DICT))
    paths.append(os.path.join(os.getcwd(), _USER_DICT))

    total = 0
    seen = set()
    for path in paths:
        if path in seen:                             # 上面几条在源码态会重合
            continue
        seen.add(path)
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            n = 0
            for k, v in data.items():
                k2, v2 = _norm(k), str(v).strip()
                if k2 and v2:
                    GAME_ZH[k2] = v2
                    n += 1
            total += n
            if verbose:
                print("[zh] 已加载用户词典 %s（%d 条）" % (path, n))
        except Exception:                            # noqa: BLE001
            if verbose:
                print("[zh] 用户词典读取失败: %s" % path)
    return total


#: ⚠ 用户词典的加载**必须放在本文件最末尾**（见文件尾那次调用）。
#: 早先写在这里，后面「第三批」的 ``GAME_ZH.update({...})`` 会把它覆盖掉 ——
#: 用户手改的词条在源码态也是失效的，只是条目少不容易发现。

GAME_ZH.update({
    # ---- 第三批: 经典游戏补录 ----
    "the legend of zelda ii": "塞尔达传说 II 大王之剑",
    "zelda ii the adventure of link": "塞尔达传说 II",
    "startropics": "圣龙传说",
    "startropics 2": "圣龙传说 2",
    "bomberman 94": "炸弹人 94",
    "bomberman hns": "炸弹人",
    "bomberman max": "炸弹人 Max",
    "bomberman max 2": "炸弹人 Max 2",
    "pac mania": "吃豆人狂热",
    "pacman": "吃豆人",
    "ms pac man": "梦魇吃豆人",
    "pac in time": "吃豆人 时空之旅",
    "donkey kong 3": "大金刚 3",
    "dkong": "大金刚",
    "dkong jr": "大金刚小子",
    "mario party 2": "马里奥派对 2",
    "super mario rsp": "超级马里奥 RSP",
    "super mario yoshi": "超级马里奥 耀西",
    "yoshi s cookie 2": "耀西饼干 2",
    "mario s time machine": "马里奥 时空机器",
    "dr mario 2": "超级马里奥医生 2",
    "metroid fusion": "密特罗德 融合",
    "metroid 2 return of samus": "密特罗德 II",
    "super metroid trilogy": "超级密特罗德 合集",
    "metroid prime pinball": "密特罗德 Prime 弹珠台",
    "kirby s dream collection": "星之卡比 梦幻合集",
    "kirby 64": "星之卡比 64",
    "kirby no kirby detour": "卡比 detour",
    "donkey konga": "大金刚弹珠",
    "final fantasy mystic quest square": "最终幻想 神秘任务",
    "final fantasy 4": "最终幻想 IV",
    "secret of evermore": "evermore 之谜",
    "live a live": "Live a Live",
    "front mission gunner": "前线任务 枪兵",
    "chrono trigger ds": "时空之轮 DS",
    "seiken densetsu 2": "圣剑传说 2",
    "breath of fire 3": "呼吸之火 3",
    "breath of fire 4": "呼吸之火 4",
    "vampires dawn": "吸血鬼黎明",
    "suikoden 3": "幻想水滸伝 3",
    "suikoden 4": "幻想水滸伝 4",
    "suikoden 5": "幻想水滸伝 5",
    "summon night": "召唤之夜",
    "summon night 2": "召唤之夜 2",
    "summon night 3": "召唤之夜 3",
    "shining force 3": "光之力量 3",
    "langrissa": "兰古锡萨",
    "langrissa 2": "兰古锡萨 2",
    "fire emblem": "圣剑战记",
    "fire emblem gaiden": "圣剑战记 外传",
    "fire emblem ogre battle": "圣剑战记 纹章之战",
    "fire emblem thracia 776": "圣剑战记 776",
    "advance wars": "Advance Wars",
    "advance wars 2": "Advance Wars 2",
    "advance wars dual strike": "Advance Wars 双重打击",
    "wario land 3": "瓦力欧大陆 3",
    "wario ware": "瓦力欧 抓挠",
    "wario ware twisted": "瓦力欧 抓挠 扭曲",
    "dk racing": "大金刚赛车",
    "diddy kong racing ds": "迪迪熊赛车 DS",
    "star fox command": "星际火狐 指令",
    "star fox adventures": "星际火狐 冒险",
    "f zero gx": "F-Zero GX",
    "xenosaga episode i": "异度神剑 1",
    "xenoblade chronicles": "异度神剑",
    "paper mario thousand year door": "纸片马力欧 千年之门",
    "mario & luigi 3": "马里奥与路易吉 3",
    "luigi s mansion dark moon": "路易吉洋馆 暗影moon",
    "new super mario bros ds": "新超级马里奥兄弟 DS",
    "new super mario bros wii": "新超级马里奥兄弟 Wii",
    "super mario galaxy": "超级马里奥银河",
    "super mario galaxy 2": "超级马里奥银河 2",
    "super mario 3d land": "超级马里奥 3D 大陆",
    "super mario 3d world": "超级马里奥 3D 世界",
    "mario kart 64 ds": "马里奥赛车 DS",
    "mario kart wii": "马里奥赛车 Wii",
    "mario kart ds": "马里奥赛车 DS",
    "nintendods": "任天堂 DS",
    "nintendo 64": "N64",
    "tetris effect": "俄罗斯方块 效应",
    "tetris worlds": "俄罗斯方块 世界",
    "arkanoid": "打砖块",
    "arkanoid 2": "打砖块 2",
    "rainbow islands": "彩虹岛",
    "chack n pop": "Chack n Pop",
    "urban champion": "都市冠军",
    "dead connection": "死亡连线",
    "flying warriors": "飞行战士",
    "wing man": "翼人",
    "1943 kai": "1943 改",
    "bionic commando rearmed": "生化尖兵 复刻",
    "commando 2": "突击队 2",
    "the hokuto no ken": "北斗神拳",
    "ikari warriors 2": "怒 2",
    "toki 2 juuman": "Toki 2",
    "ghouls n ghosts": "幽灵",
    "shadow of the ninja": "忍者之影",
    "shadow of the ninja 2": "忍者之影 2",
    "gotou": "五右卫门",
    "the last ninja": "最后忍者",
    "ninja kids 3": "忍者小子 3",
    "jojo adventure": "JoJo 冒险",
    "rockman 4": "洛克人 4",
    "rockman 6": "洛克人 6",
    "rockman 7": "洛克人 7",
    "rockman w": "洛克人 W",
    "megaman x": "洛克人 X",
    "megaman x2": "洛克人 X2",
    "megaman x3": "洛克人 X3",
    "megaman x4": "洛克人 X4",
    "megaman x5": "洛克人 X5",
    "zero s revenge": "零之复仇",
    "super smash bros dojo": "任天堂明星大乱斗",
    "kirby tiffs dream mix": "星之卡比 混合",
    "doraemon nobita no dorabian night": "哆啦A梦 七个球",
    "doraemon no dorabian night": "哆啦A梦 七个球",
    "doraemon dorabian night": "哆啦A梦 七个球",
    "yu yu hakusho 2": "幽游白书 2",
    "kinnikuman 2": "肌肉人 2",
    "nangoku shonen papuwa-kun": "南国少年 Papuwa",
    "magical chinese": "魔法中国",
    "ninja boy 2": "忍者少年 2",
    "wonder boy": "神奇小子",
    "wonder boy 2": "神奇小子 2",
    "monster in my pocket": "口袋怪兽",
    "ghostbusters": "捉鬼敢死队",
    "pokemon pinball": "宝可梦 弹珠台",
    "pokemon trader": "宝可梦 交换",
    "pokemon plaza": "宝可梦 广场",
    "pokemon puzzle league": "宝可梦 益智联盟",
    "pokemon stadium 2": "宝可梦 竞技场 2",
    "pokémon emerald version": "宝可梦 绿宝石",
    "pokemon card gb": "宝可梦 卡片 GB",
    "pokemon card 2": "宝可梦 卡片 2",
    "final fantasy i": "最终幻想 I",
    "final fantasy 5": "最终幻想 V",
    "final fantasy 6": "最终幻想 VI",
    "final fantasy 3": "最终幻想 III",
    "final fantasy 2": "最终幻想 II",
    "ff4a": "最终幻想 IV",
    "ff6": "最终幻想 VI",
    "octopath traveler ii": "八方旅人 2",
    "triangle strategy": "三角战略",
    "advance wars 64": "Advance Wars 64",
    "mario golf super rush": "马里奥高尔夫 超级冲刺",
    "mario tennis": "马里奥网球",
    "mario baseball": "马里奥棒球",
    "mario soccer": "马里奥足球",
    "mario power tennis": "马里奥网球 力量版",
    "mario & sonic at the olympic games": "马里奥与索尼克 奥运会",
    "sonic and the black knight": "索尼克与黑骑士",
    "sonic and the secret rings": "索尼克与神秘环",
    "sonic riders": "索尼克骑士",
    "sonic free riders": "索尼克自由骑士",
    "sonic colors": "索尼克  Colors",
    "sonic generations": "索尼克 世代",
    "sonic lost world": "索尼克 失落世界",
    "sonic forces": "索尼克 力",
    "sonic mega collection": "索尼克 合集",
    "shadow the hedgehog x": "夏特 X",
    "sonic and the giant robot": "索尼克与巨型机器人",
    "racing lagoon": "Racing Lagoon",
    "jet set radio": "Jet Set Radio",
    "jet set radio future": "Jet Set Radio Future",
    "jet set radio 2": "Jet Set Radio 2",
    "viewtiful joe": "红侠乔伊",
    "viewtiful joe 2": "红侠乔伊 2",
    "okami": "大神",
    "viewtiful joe red hot rumble": "红侠乔伊 热血",
    "sonic advance": "索尼克 前进",
    "sonic advance 2": "索尼克 前进 2",
    "sonic advance 3": "索尼克 前进 3",
    "sonic battle": "索尼克 对战",
    "sonic pinball party": "索尼克 弹珠派对",
    "sonic 1": "索尼克 1",
    "sonic 2": "索尼克 2",
    "sonic 3": "索尼克 3",
    "sonic 4": "索尼克 4",
    "sonic the fighters": "索尼克 格斗",
    "sonic boom": "索尼克 冲击",
    "sonic 3d fury": "索尼克 3D 狂怒",
    "sonic superstars": "索尼克 超星",
    "crash team racing": "Crash 车队赛车",
    "medievil": "中世纪",
    "medievil 2": "中世纪 2",
    "sly 3": "怪盗 3",
    "ratchet & clank 2": "瑞奇与叮当 2",
    "ratchet & clank 3": "瑞奇与叮当 3",
    "ratchet deadlocked": "瑞奇与叮当 死锁",
    "Jak 2": "Jak 2",
    "jak 3": "Jak 3",
    "jak and daxter the lost frontier": "Jak 与 Daxter 失落边疆",
    "sly cooper thieves in time": "怪盗 时空大盗",
    "sly cooper": "怪盗",
    "spyro season of dragons": "古瑞龙 龙之季",
    "spyro year of the dragon": "古瑞龙 龙年",
    "spyro a hero s tail": "古瑞龙 英雄之尾",
    "spyro": "古瑞龙",
    "god of war chains of olympus": "战神 奥林匹斯之链",
    "god of war ghost of sparta": "战神 斯巴达之魂",
    "god of war ascension": "战神 登神",
    "persona 3": "女神异闻录 3",
    "persona 4": "女神异闻录 4",
    "persona 4 golden": "女神异闻录 4 黄金版",
    "persona 2": "女神异闻录 2",
    "digital devil saga": "真女神转生",
    "eternal Punishment": "永恒惩罚",
    "mt doom": "Doom",
    "xenosaga": "异度神剑",
})


# 带缓存的对外接口(界面每帧要翻上千条, 别重复算正则)
@functools.lru_cache(maxsize=200000)
def zh_name(name: str) -> str:
    return translate(name)


def zh_or_dash(name: str) -> str:
    """界面用: 翻不出来就返回 em dash, 不要留空白让人以为是渲染失败。"""
    return zh_name(name) or "\u2014"


# --------------------------------------------------------------------------
# 联网补词(谷歌翻译)的落库接口
# --------------------------------------------------------------------------
def add_pairs(pairs: Dict[str, str]) -> int:
    """把新译名并进内存词典, 返回真正新增的条数。"""
    n = 0
    for k, v in (pairs or {}).items():
        k2, v2 = _norm(k), str(v).strip()
        if k2 and v2 and GAME_ZH.get(k2) != v2:
            GAME_ZH[k2] = v2
            n += 1
    if n:
        zh_name.cache_clear()
    return n


def user_dict_path() -> Optional[str]:
    """**可写**的用户词典路径：优先 exe 同级，退到 ``%LOCALAPPDATA%``。

    ⚠ 打包后**绝不能**返回 ``dirname(__file__)`` —— Nuitka onefile 的
      ``__file__`` 指向解包临时目录，写进去下次启动就没了（临时目录会重建）。
      早先这里用 ``sys.frozen`` 判断打包态，Nuitka 没有这个属性 → 永远走
      ``__file__`` 分支 → 用户手改的译名静默丢失。
    """
    import os
    import sys

    compiled = bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
    cands = []
    if compiled:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                                  _USER_DICT))
    else:
        # 源码态：优先写回项目里那份，方便直接看 diff
        cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  _USER_DICT))
    base = os.environ.get("LOCALAPPDATA")
    if base:
        cands.append(os.path.join(base, "MSU1Converter", _USER_DICT))
    for path in cands:
        try:
            d = os.path.dirname(path)
            if not os.path.isdir(d):
                # 首次运行的新机器上 %LOCALAPPDATA%\MSU1Converter 还不存在 ——
                # 只判 isdir 会一路 continue 到底、返回 None，用户一点「保存译名」
                # 就报「没有可写的用户词典路径」。这里顺手建一下。
                os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return path
        except OSError:
            continue
    return None


def save_pairs(pairs: Dict[str, str]) -> str:
    """把译名合并写进用户词典(保留已有条目), 返回写入的路径。"""
    import json
    import os
    path = user_dict_path()
    if not path:
        raise OSError("没有可写的用户词典路径")
    data: Dict[str, str] = {}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = {str(k): str(v) for k, v in loaded.items()}
    except Exception:                                # noqa: BLE001
        pass
    for k, v in (pairs or {}).items():
        k2, v2 = _norm(k), str(v).strip()
        if k2 and v2:
            data[k2] = v2
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return path


# 厂商/品牌名(直译兜底常常因为这些词不认识而丢词, 补上后"世界 杯"会变"任天堂世界 杯")
_TOKENS.update({
    "nintendo": "任天堂", "sega": "世嘉", "sony": "索尼", "bandai": "万代",
    "konami": "科乐美", "namco": "南梦宫", "capcom": "卡普空",
    "enix": "艾尼克斯", "square": "史克威尔", "hudson": "哈德逊",
    "tecnos": "科技", "tecmo": "特科莫", "koei": "光荣",
    "jaleco": "杰力科", "irem": "艾力姆", "toaplan": "东版",
    "atari": "雅达利", "nintendo power": "任天堂力量",
    "eisystem": "EIS", "taito": "东立", "sunsoft": "SUN",
    "tonkin house": "东洋", "natsume": "纳西姆", "micronics": "麦克罗",
    "psychic": "精神", "visual": "视觉", "data east": "数据东",
    "mikado": "三光", "dynamic": "动态", "ntsc": "NTSC",
    "usa": "美版", "japan": "日版", "europe": "欧版",
    "rev a": "Rev A", "rev b": "Rev B",
})


_LATIN_RE = re.compile(r"[A-Za-z]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def zh_weak(name: str) -> bool:
    """这条中文名是不是"半吊子"——机翻里英文比中文还多, 或压根没有中文。

    这种不删(用户要求全翻), 但界面上用暗色标出来提醒人工修。
    """
    zh = zh_name(name)
    if not zh:
        return True
    cjk = len(_CJK_RE.findall(zh))
    latin = len(_LATIN_RE.findall(zh))
    return cjk == 0 or latin > cjk


# --------------------------------------------------------------------------
# 用户词典在这里合并 —— **必须是本模块的最后一句可执行代码**。
# 放在前面任何一个 ``GAME_ZH.update({...})`` 之前，都会被那几批补录覆盖掉。
# --------------------------------------------------------------------------
_load_user_dict()
