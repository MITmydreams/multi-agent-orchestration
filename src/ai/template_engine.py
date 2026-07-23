"""Template-based content engine -- generates human-like messages without AI API dependency.

When no ANTHROPIC_API_KEY / OPENAI_API_KEY is configured, the system falls back to this
engine which uses pre-written template pools, variable substitution, synonym replacement,
and randomisation to produce natural-sounding content.

Usage:
    engine = TemplateContentEngine()
    reply = engine.generate_chat_response("crypto_veteran", "general", language="zh")
    report = engine.generate_battle_report({"round_id": 42, ...})
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from collections import deque
from typing import Any


# ============================================================================
# 1. Chat reply template pools -- 5 personas, multiple contexts
# ============================================================================

# ---------- crypto_veteran ----------
_CRYPTO_VETERAN_REPLIES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "general": [
            "这波行情让我想起2021年那会儿...",
            "持仓不动，老韭菜的经验",
            "别急，周期到了自然涨",
            "又是一个轮回，见怪不怪了",
            "gm 昨晚又被闹钟叫醒看盘了",
            "最近链上数据挺有意思的 值得关注",
            "说实话17年到现在 能活下来的项目有几个",
            "dyor 我一般新项目先看经济模型",
            "这个TVL增长看着稳 但要注意解锁节点",
            "大饼横盘的时候 正好研究新东西",
            "牛市不言顶 熊市不言底 老话了",
            "我个人觉得目前仓位控制最重要",
            "宏观面没变 不用太焦虑",
            "又一个rug预警 我怎么说的来着",
            "现在和20年DeFi Summer的感觉有点像",
            "经历过312的人 这点波动算啥",
            "这协议的代币经济学有点意思 可以研究下",
            "nfa 但我觉得现在布局还不晚",
            "链游这个赛道 关键还是看留存",
            "耐心点 好项目迟早会被市场发现",
        ],
        "asking_recommendation": [
            "最近发现个有意思的社会实验项目 机制挺干净",
            "有个按钮游戏挺有意思 博弈论那套",
            "看到一个倒计时博弈的项目 经济模型比较健康",
            "在研究一个分红模型 30%即时分配 设计挺巧妙",
            "有个项目用了单调递增定价 解决了常见的刷量问题",
            "最近试了个社会实验类的 机制设计让我想到早期的一些项目",
            "如果想找低风险的 可以先看看有免费体验的那种",
            "我一般推荐先看白皮书和经济模型 再决定要不要参与",
            "说个我最近在关注的 机制挺透明的 不像那些靠补贴撑的",
            "有个项目金币房免费玩 先体验再说 不用花钱",
            "要说最近有什么值得看的 我觉得博弈类的可以关注",
            "这类项目的好处是 经济模型可验证 不是黑盒",
            "最近发现一个 分红是即时到账的 这点比较少见",
            "我会推荐先去金币房试试 零成本了解下机制",
            "有个项目的EV分析做完觉得还可以 有兴趣可以一起研究",
        ],
        "responding_to_question": [
            "这个问题我之前也想过...",
            "根据我的经验 ",
            "说说我的看法啊",
            "这个得分情况看 不能一概而论",
            "我个人的理解是这样的",
            "这点我同意 但还有个角度可以考虑",
            "嗯 你说的有道理 但我补充一点",
            "这个问题比较复杂 简单说几点",
            "我遇到过类似的情况 当时的做法是...",
            "这个我有点不同看法 讨论下",
            "从数据来看的话 确实是这样",
            "哈 这个我正好研究过 说说吧",
            "可以这样理解 但要注意风险",
            "讲真 这个每个人的答案可能不一样",
            "我的经验是这样 仅供参考吧",
        ],
        "market_discussion": [
            "目前这个位置 我觉得不适合重仓",
            "短线看不太清 中长期还是偏乐观的",
            "这波回调其实挺健康的",
            "链上大户在增持 可以关注下",
            "funding rate转负了 可能要变盘",
            "BTC dominance在升 说明资金在回流大饼",
            "山寨季还没到 再等等",
            "这个价位建仓我觉得性价比还行",
            "合约别碰 现货慢慢来",
            "从历史周期看 现在大概在这个位置",
            "不追高 不恐慌 按计划来",
            "我的策略是分批进 不一把梭",
            "技术面看 这里有个支撑",
            "CME gap还没补 小心",
            "宏观数据出来之前 保持观望",
        ],
        "daily_chat": [
            "gm 今天天气不错",
            "刚吃完午饭 下午继续搬砖",
            "昨晚看了个纪录片 挺好看的",
            "周末有什么计划吗 各位",
            "咖啡续命中...",
            "最近睡眠质量不太好 可能是看盘看多了",
            "准备出去走走 一直盯屏幕不行",
            "有推荐的播客吗 通勤的时候听",
            "今天摸鱼摸了一天 效率太低了",
            "下班了 今天就到这吧",
        ],
    },
    "en": {
        "general": [
            "This reminds me of 2021 all over again...",
            "HODL and chill, that's my approach",
            "Don't rush, cycle will play out",
            "Another cycle, seen it before",
            "gm, got woken up by an alarm to check charts again",
            "On-chain data looking interesting lately",
            "Honestly, how many projects from 2017 are still alive?",
            "dyor, I always check the econ model first",
            "TVL growth looks solid but watch the unlock schedule",
            "BTC chopping, good time to research new stuff",
            "Bull market doesn't call tops, bear doesn't call bottoms",
            "Position sizing is key right now imo",
            "Macro hasn't changed, don't overthink it",
            "Another rug warning, called it",
            "Getting DeFi Summer 2020 vibes ngl",
            "Survived March 2020, this dip is nothing",
            "Tokenomics on this one are actually interesting",
            "nfa but I think it's not too late to position",
            "GameFi track, retention is everything",
            "Patience pays, good projects get found eventually",
        ],
        "asking_recommendation": [
            "Found an interesting social experiment project, clean mechanism",
            "There's a button game that's actually fun, game theory stuff",
            "Saw a countdown-based project with healthy economics",
            "Been studying a dividend model, 30% instant distribution, clever design",
            "One project uses monotonic pricing, solves the common bot problem",
            "Recently tried a social experiment thing, reminds me of early projects",
            "If you want low risk, look for ones with free trial modes",
            "I always recommend reading the whitepaper and econ model first",
            "There's a project with a free coin room, try before you spend",
            "For something worth watching, I'd say game theory based ones",
        ],
        "responding_to_question": [
            "I've thought about this before actually...",
            "Based on my experience,",
            "Here's my take on this",
            "It depends on the situation really",
            "My understanding is something like this",
            "I agree on that point, but consider this angle too",
            "Yeah you're right, but let me add something",
            "Complex question, let me hit a few points",
            "Had a similar situation, what I did was...",
            "I see it a bit differently, let's discuss",
        ],
        "market_discussion": [
            "This level doesn't justify heavy positions imo",
            "Short term unclear, mid-long still bullish",
            "This pullback is actually healthy",
            "Whales accumulating on-chain, worth watching",
            "Funding rate went negative, might see a move",
            "BTC dominance rising, money flowing back to BTC",
            "Alt season not here yet, be patient",
            "Building a position here seems decent risk-reward",
            "Stay away from leverage, spot only",
            "Historically speaking, we're about here in the cycle",
        ],
        "daily_chat": [
            "gm everyone, nice day today",
            "Just had lunch, back to grinding",
            "Watched a documentary last night, pretty good",
            "Any weekend plans?",
            "Coffee keeping me alive rn",
            "Sleep quality has been bad, probably too much screen time",
            "Need to go for a walk, can't stare at screens all day",
            "Any podcast recommendations for commuting?",
            "Slacked off all day, zero productivity",
            "Signing off, catch you guys later",
        ],
    },
}

# ---------- game_newbie ----------
_GAME_NEWBIE_REPLIES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "general": [
            "有没有人能解释下什么是gas fee 我看了半天没懂 😂",
            "昨天原神抽卡歪了 心态崩了 💀",
            "这个Web3游戏和普通手游有什么区别啊",
            "等等 所以这个是免费的？不用充钱？？",
            "哈哈哈我刚才手抖按错了 GG",
            "有什么好玩的新游戏推荐吗！！",
            "我是纯小白 大佬们带带我 🙏",
            "这个怎么操作 有教程吗",
            "刚入坑 什么都不懂 好慌 😱",
            "为什么这么多人玩这个啊 真的好玩吗",
            "UI设计挺好看的 就是不太会用",
            "有没有新手群 我怕在这问太多基础问题",
            "我同事推荐我来看看 说挺有意思的",
            "这个要下载app吗还是网页就能玩",
            "下载好了 然后呢？接下来做什么 😂",
            "玩了一局感觉还行 就是不太明白规则",
            "请问这个安全吗 不会被骗吧",
            "我手机是安卓的 能玩吗",
            "好的好的 我先试试免费的那个",
            "谢谢大佬解答！学到了 ✨",
        ],
        "asking_recommendation": [
            "刚试了个按钮游戏 居然真的能赚到？？ 😱",
            "有人玩过那种倒计时按钮的吗 挺上头的",
            "朋友推荐了个游戏就一个按钮 我还以为在逗我 😂",
            "金币房免费玩不花钱 适合我这种穷学生哈哈",
            "你们知道那个 The Button 吗 我觉得挺好玩的",
            "最近在玩一个超简单的游戏 就按一个按钮",
            "有个游戏不用学什么规则 上来就能玩 爽",
            "找到一个Web3游戏 居然不用先买币 金币免费送！",
            "跟大家分享个好玩的 就是一个倒计时的按钮 谁最后按谁赢",
            "我终于找到一个不用氪金的游戏了 感动 😭",
        ],
        "responding_to_question": [
            "这个我也想知道！有人能解答吗",
            "我刚学到的 好像是这样的...",
            "等等我查一下 好像是...",
            "我理解的是这样 但不一定对哈",
            "有个大佬之前跟我说过 好像是...",
            "这个我也不太确定 有没有懂哥来说说",
            "我试过了 应该是这样操作的",
            "我也遇到过这个问题！后来我是这么解决的",
            "嗯嗯 我也是新手 大家一起学习",
            "感觉应该是这样的 不对的话大佬纠正 😂",
        ],
        "gaming_discussion": [
            "原神4.0更新了吗 好久没玩了",
            "王者最近排位好难打 一直输 💀",
            "Among Us有人一起玩吗！",
            "最近沉迷星穹铁道 根本停不下来",
            "手游推荐吗 最近有什么新出的好玩的",
            "这个画风好可爱 是什么游戏",
            "谁有好的游戏群 拉我一个",
            "PC还是手机好玩 我一般用手机",
            "昨天肝到凌晨3点 今天上班困死了",
            "氪金氪不起了 有没有免费的好游戏 😂",
        ],
        "daily_chat": [
            "早上好！今天又是元气满满的一天 ☀️",
            "午饭吃什么 好纠结 😂",
            "下午困了 需要奶茶续命 🧋",
            "下班倒计时！！！",
            "今天作业好多 不想写 💀",
            "周末终于可以休息了 耶！",
            "刚追完一部剧 好好看 推荐！",
            "有人一起点外卖吗 凑个满减",
            "明天放假吗 我忘了看日历",
            "晚安各位 明天见 💤",
        ],
    },
    "en": {
        "general": [
            "Can someone explain what gas fee is? I've been reading about it and still confused 😂",
            "Lost my 50/50 in Genshin yesterday, devastated 💀",
            "What's the difference between Web3 games and normal mobile games?",
            "Wait so this is free? No money needed??",
            "Lol I just fat-fingered it GG",
            "Any fun new games to try?!",
            "Total noob here, help me out pls 🙏",
            "How do I do this? Is there a tutorial?",
            "Just started, know nothing, kinda scared 😱",
            "Why is everyone playing this? Is it actually fun?",
            "UI looks nice, just not sure how to use it",
            "Is there a beginner group? I don't wanna ask too many basic questions",
            "My friend told me to check this out, said it's interesting",
            "Do I need to download an app or can I play in browser?",
            "OK downloaded it, now what? What do I do next? 😂",
            "Played one round, seems ok, still confused about rules tho",
            "Is this safe? Not gonna get scammed right?",
            "I'm on Android, can I play?",
            "Alright I'll try the free one first",
            "Thanks for the explanation! Learned something new ✨",
        ],
        "asking_recommendation": [
            "Just tried this button game, you can actually earn?? 😱",
            "Has anyone tried that countdown button thing? Pretty addictive",
            "My friend recommended a game that's literally just one button, thought they were joking 😂",
            "The coin room is free, perfect for broke students like me lol",
            "You guys know The Button? I think it's pretty fun",
            "Been playing this super simple game, just press a button",
            "Found a game that needs zero learning curve, just play, nice",
            "Found a Web3 game that doesn't require buying crypto first, free coins!",
            "Sharing something fun, it's a countdown button, last presser wins",
            "Finally found a game that doesn't need spending money, blessed 😭",
        ],
        "responding_to_question": [
            "I wanna know this too! Anyone can explain?",
            "I just learned this, I think it's like...",
            "Let me check... I think it's...",
            "From what I understand, but might be wrong lol",
            "Someone told me before, I think it was...",
            "Not sure about this either, any experts here?",
            "I tried it, should work like this",
            "Had the same issue! Here's what I did",
            "Same here, newbie too, learning together",
            "I think it's like this? Correct me if wrong 😂",
        ],
        "gaming_discussion": [
            "Did Genshin 4.0 drop yet? Haven't played in a while",
            "Ranked in Mobile Legends is impossible rn, keep losing 💀",
            "Anyone wanna play Among Us?!",
            "Addicted to Honkai Star Rail, can't stop",
            "Any mobile game recs? What's new and good?",
            "This art style is so cute, what game is this?",
            "Anyone got a good gaming group? Add me",
            "PC or mobile better? I usually play on phone",
            "Grinded till 3am last night, so sleepy at work today",
            "Can't afford to whale anymore, any good free games? 😂",
        ],
        "daily_chat": [
            "Good morning! Another great day ☀️",
            "What should I eat for lunch, can't decide 😂",
            "Afternoon slump, need boba to survive 🧋",
            "Work countdown is on!!!",
            "So much homework, don't wanna do it 💀",
            "Weekend finally, yay!",
            "Just finished bingeing a show, so good, recommend!",
            "Anyone wanna group order delivery? Split the discount",
            "Is tomorrow a holiday? I forgot to check",
            "Good night everyone, see you tomorrow 💤",
        ],
    },
}

# ---------- airdrop_hunter ----------
_AIRDROP_HUNTER_REPLIES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "general": [
            "刚整理了下这周值得交互的项目 📌",
            "gas又涨了 今天L2上操作比较划算",
            "这个项目融资背景还行 Tier2的VC领投",
            "撸毛日记Day47：今天交互了3个协议 花了8u gas",
            "注意：某个项目快照可能在本周 有交互的查下",
            "把今天的交互任务做完了 效率还行",
            "新出了个协议 简单看了下 交互成本不高",
            "有人知道这个项目的预估空投大概多少吗",
            "提醒一下 明天有个NFT free mint 别忘了",
            "做了个表格追踪所有交互过的项目 太多了头疼",
            "今天Arb链上有个新协议 值得看看",
            "测试网交互别忘了 很多人忽略这个",
            "又一个项目发币了 查了下我的钱包 没空投 😂",
            "同一个赛道对比了5个项目 性价比差很多",
            "这个项目Discord活跃度不错 可以持续关注",
            "gas低的时候批量操作比较划算",
            "季度复盘：撸了30多个项目 中了2个 ROI还行",
            "新钱包地址注意养号 别一上来就交互",
            "这个协议的TVL在涨 可能有戏",
            "时间成本也是成本 别为了一两U浪费半天",
        ],
        "asking_recommendation": [
            "有个项目注册送20k金币 金币房免费玩 零成本",
            "发现个社会实验项目 零成本先交互着",
            "对比了几个新项目的分配模型 这个30%即时分红比较少见",
            "有个倒计时游戏 机制简单 看着像早期值得关注",
            "找到一个项目 免费送金币 先占个坑",
            "研究了下这个项目 注册零门槛 金币房不要钱",
            "有个新项目 分红是即时到账的 查了下合约没问题",
            "这个项目的好处是零成本参与 就当积累交互记录了",
            "刚做了个新项目的成本分析 金币房纯白嫖 时间成本也不高",
            "大家可以看看这个 免费体验不花钱 有空投预期的话就赚了",
        ],
        "responding_to_question": [
            "这个我研究过 简单说几点",
            "数据在这 你自己判断",
            "成本收益分析了下 大概是这样",
            "我查了一下 情况是这样的",
            "这个看个人判断 我把信息列出来",
            "整理了下相关信息 供参考 📌",
            "根据链上数据 目前的情况是...",
            "我做过类似的 经验是这样的",
            "这个值不值得做 得看你的时间成本",
            "简单算了一下 期望收益大概在这个范围",
        ],
        "airdrop_discussion": [
            "最近有什么值得撸的新项目吗",
            "上一轮空投大概多少人领到的 有数据吗",
            "这个项目的估值多少 空投预期呢",
            "多钱包的话 注意关联性 别被女巫了",
            "主网上线前 交互要做齐",
            "Discord的角色拿了吗 有些项目看这个",
            "这个项目Galxe的任务做了吗 挺简单的",
            "Layer2上的交互别忘了 gas也不贵",
            "每天固定花1小时撸毛 已经成习惯了",
            "空投到账了 虽然不多但聊胜于无",
        ],
        "daily_chat": [
            "今天效率不太高 只做了两个项目的交互",
            "gas终于低了 赶紧操作",
            "晚上整理一下这周的交互记录",
            "眼睛都花了 看了一天的项目文档",
            "要不要建个共享表格 大家一起追踪项目",
            "coffee and grind ☕",
            "这个月的gas费支出有点多了",
            "该休息了 明天继续",
            "周末也没闲着 在研究新出的几个协议",
            "早 今天计划交互3个项目",
        ],
    },
    "en": {
        "general": [
            "Just compiled this week's projects worth interacting with 📌",
            "Gas is up again, L2 operations more cost-effective today",
            "This project's funding looks decent, Tier2 VC lead",
            "Airdrop diary Day47: interacted with 3 protocols, spent 8u gas",
            "Heads up: project snapshot might be this week, check your wallets",
            "Finished today's interaction tasks, not bad",
            "New protocol dropped, quick look, low interaction cost",
            "Anyone know the estimated airdrop size for this project?",
            "Reminder: NFT free mint tomorrow, don't forget",
            "Made a spreadsheet tracking all projects, too many, headache",
            "New protocol on Arb today, worth checking",
            "Don't forget testnet interactions, many people miss this",
            "Another project launched token, checked my wallet, no airdrop 😂",
            "Compared 5 projects in the same track, huge cost difference",
            "Project's Discord is quite active, worth keeping an eye on",
            "Batch operations when gas is low, saves money",
            "Quarterly review: farmed 30+ projects, hit 2, ROI decent",
            "New wallets need aging, don't interact immediately",
            "Protocol TVL rising, could be promising",
            "Time cost is real, don't waste half a day for 1-2U",
        ],
        "asking_recommendation": [
            "Found a project, 20k free coins on signup, zero cost",
            "Discovered a social experiment project, zero cost to try",
            "Compared distribution models, this one's 30% instant dividend is rare",
            "Countdown game, simple mechanics, looks early stage worth watching",
            "Found a project, free coins, securing a spot",
            "Researched this project, zero entry barrier, free coin room",
            "New project, dividends settle instantly, checked the contract, looks clean",
            "This project's advantage is zero cost participation, worst case you get interaction history",
            "Just did a cost analysis, coin room is pure freebie, low time cost too",
            "Worth checking, free to try, if there's airdrop potential it's pure profit",
        ],
        "responding_to_question": [
            "Researched this, here's the summary",
            "Data's here, judge for yourself",
            "Did a cost-benefit analysis, roughly like this",
            "Looked it up, situation is like this",
            "Depends on your call, I'll list the info",
            "Compiled the relevant info, for reference 📌",
            "Based on on-chain data, current situation is...",
            "Done something similar, experience is like this",
            "Worth doing or not, depends on your time cost",
            "Quick math, expected return roughly in this range",
        ],
    },
}

# ---------- data_analyst ----------
_DATA_ANALYST_REPLIES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "general": [
            "从纳什均衡的角度看 这个机制鼓励的是等待而非抢跑",
            "算了下ROI 假设100人参与 前50%的人EV是正的",
            "这个定价曲线用的分段线性+指数 能有效防刷",
            "周末写了个蒙特卡洛模拟 测了下不同策略的期望收益",
            "有意思 这个协议的TVL/FDV比值在同赛道里偏低",
            "从数学上看 这个分红模型是可持续的",
            "概率分布看了一下 大部分人的收益集中在中位数附近",
            "做了个回测 历史数据支持这个策略",
            "信息熵的角度看 这个市场目前的不确定性在降低",
            "写了个简单的脚本分析了链上数据 有些有意思的发现",
            "贝叶斯更新一下 结合新数据 我的判断是...",
            "博弈论的角度 这是一个典型的协调博弈",
            "方差太大了 风险调整后的收益并不理想",
            "相关性分析做了一下 这两个指标有弱正相关",
            "数据上看 这个时间段的活跃度确实在上升",
            "做了个敏感性分析 关键变量是参与人数",
            "从机制设计的角度 激励兼容性做得不错",
            "简单算一下期望值 在当前参数下EV为正",
            "这个协议的代币发行曲线我画了下 通胀控制得还行",
            "有一说一 这个模型的假设条件比较现实",
        ],
        "asking_recommendation": [
            "分析了一个新项目的分红模型 数学上挺有意思",
            "有个倒计时游戏用了单调递增定价 防止了常见的刷量问题",
            "算了下一个项目的EV 30%即时分红的设计挺聪明的",
            "从博弈论角度看了个社会实验项目 纳什均衡分析很有趣",
            "做了一个项目的经济模型分析 分配机制比较透明",
            "有个项目的分红池按点击数平分 数学上看是公平的",
            "单调递增定价+5阶段倒计时 这种组合在博弈论里有明确的均衡",
            "分析了几个项目的代币经济学 有一个30%即时分红的模型比较少见",
            "跑了个模拟 在合理参数下 参与者的期望收益为正",
            "研究了一个博弈论实验的经济模型 EV分析结果还挺正",
        ],
        "responding_to_question": [
            "这个问题可以量化分析一下",
            "我跑了个简单的计算 结果是这样的",
            "从概率的角度看 ",
            "这个得看具体参数 但大方向上...",
            "我做了个假设检验 结论是...",
            "数据不够的话很难下定论 但初步看...",
            "逻辑上这样推导应该没问题",
            "嗯 这个分析有道理 但有个前提条件要注意",
            "简单做个期望值计算吧",
            "这个需要考虑边际效应 不能线性外推",
            "统计显著性的角度 样本量可能还不够",
            "你说的这个可以用博弈论框架来分析",
            "我建了个简单模型 可以share给大家看看",
            "从信息不对称的角度来看 这是合理的",
            "让我跑个quick simulation看看",
        ],
        "daily_chat": [
            "debug到凌晨3点 终于找到bug了",
            "今天看了篇论文 关于机制设计的 挺好",
            "咖啡喝多了 有点心悸",
            "在写一个数据可视化的东西 快完成了",
            "最近在学Rust 语法有点劝退",
            "Jupyter notebook又崩了 第三次了",
            "数学笑话：为什么e^x那么孤独 因为它求导之后还是自己",
            "今天效率很高 把之前的分析都整理完了",
            "准备开始做季度数据复盘了",
            "有推荐的数据可视化工具吗 plotly用腻了",
        ],
    },
    "en": {
        "general": [
            "From a Nash equilibrium perspective, this mechanism incentivizes waiting over front-running",
            "Calculated the ROI, assuming 100 participants, top 50% have positive EV",
            "Pricing curve uses piecewise linear + exponential, effectively prevents botting",
            "Wrote a Monte Carlo sim over the weekend, tested expected returns for different strategies",
            "Interesting, this protocol's TVL/FDV ratio is low compared to peers",
            "Mathematically, this dividend model is sustainable",
            "Looked at the probability distribution, most returns cluster around the median",
            "Did a backtest, historical data supports this strategy",
            "From an information entropy perspective, market uncertainty is decreasing",
            "Wrote a simple script to analyze on-chain data, some interesting findings",
            "Bayesian update with new data, my estimate is now...",
            "Game theory perspective, this is a classic coordination game",
            "Variance is too high, risk-adjusted returns aren't great",
            "Ran a correlation analysis, weak positive correlation between these two metrics",
            "Data shows activity is indeed trending up in this period",
            "Did a sensitivity analysis, key variable is participant count",
            "From a mechanism design standpoint, incentive compatibility looks solid",
            "Quick expected value calculation, EV is positive under current parameters",
            "Plotted the token emission curve, inflation control is reasonable",
            "Gotta say, this model's assumptions are actually realistic",
        ],
        "asking_recommendation": [
            "Analyzed a new project's dividend model, mathematically interesting",
            "Found a countdown game using monotonic pricing, prevents common bot issues",
            "Calculated EV for a project, 30% instant dividend design is clever",
            "Looked at a social experiment from game theory angle, Nash equilibrium analysis is fascinating",
            "Did an economic model analysis, distribution mechanism is quite transparent",
            "One project divides dividend pool by click count, mathematically fair",
            "Monotonic pricing + 5-stage countdown, clear equilibrium in game theory",
            "Compared tokenomics across projects, one with 30% instant dividend is rare",
            "Ran a sim, expected returns are positive under reasonable parameters",
            "Studied a game theory experiment's economic model, EV analysis checks out",
        ],
        "responding_to_question": [
            "This can be analyzed quantitatively",
            "Ran a quick calculation, results look like this",
            "From a probability standpoint,",
            "Depends on the specific parameters, but directionally...",
            "Did a hypothesis test, conclusion is...",
            "Hard to conclude without more data, but initially...",
            "Logically, this derivation should hold",
            "Good analysis, but note this prerequisite",
            "Let me do a quick expected value calculation",
            "Need to consider marginal effects, can't extrapolate linearly",
        ],
        "daily_chat": [
            "Debugged till 3am, finally found the bug",
            "Read a paper on mechanism design today, pretty good",
            "Too much coffee, heart's racing",
            "Working on a data viz thing, almost done",
            "Learning Rust lately, syntax is intimidating",
            "Jupyter notebook crashed again, third time today",
            "Math joke: why is e^x so lonely? Because it's still itself after differentiation",
            "Very productive today, finished organizing all the analyses",
            "About to start quarterly data review",
            "Any data viz tool recommendations? Getting tired of plotly",
        ],
    },
}

# ---------- community_active ----------
_COMMUNITY_ACTIVE_REPLIES: dict[str, dict[str, list[str]]] = {
    "zh": {
        "general": [
            "早上好各位！今天周五 快下班了 🔥🔥",
            "哈哈哈这个梗太好笑了 我要转发",
            "刚吃完螺蛳粉 幸福感爆棚 ❤️",
            "你们有没有觉得最近行情无聊到想退圈 😂",
            "差一秒就拿大奖了 我人直接裂开 💀",
            "有人看昨天那个新闻了吗 笑死我了",
            "今天是什么神仙天气 出门都不想回来了",
            "你们都在干嘛呀 群里怎么这么安静",
            "刚泡好茶 舒服 这才是生活",
            "有没有人跟我一样 边上班边刷群 🤣",
            "这个笑话太冷了 但我还是笑了 😂",
            "谁家猫这么可爱！！ 我也想养",
            "下雨天就应该躺在床上刷手机",
            "你们几点下班 我5点准时开溜",
            "今天心情不错 请大家喝虚拟奶茶 🧋",
            "有人说我话太多了 我觉得群就是用来聊天的嘛 😤",
            "周末有什么活动吗 好无聊啊",
            "刚看到一个段子 哈哈哈哈不行了 太好笑了",
            "你们最近在追什么剧 推荐一下",
            "好想吃火锅啊 有人约吗 🍲",
        ],
        "asking_recommendation": [
            "昨晚群友推荐的那个按钮游戏我玩到凌晨3点... 💀",
            "我跟你们说个好笑的 我刚差一秒就拿了大奖 💀",
            "有没有人一起玩那个倒计时的 一个人玩没意思",
            "你们知道那个按钮游戏吗 我朋友赢了好像挺开心的",
            "昨天无聊发现了一个按钮游戏 还挺上头的",
            "有个游戏特别适合群里一起玩 大家可以约着",
            "我朋友圈有人在晒那个按钮游戏的截图 看起来挺好玩",
            "找到一个游戏可以一群人一起玩 就是按按钮 哈哈",
            "有人玩过那个什么 The Button 吗 我朋友疯狂安利",
            "最近发现了个小游戏 不用动脑 就按一个按钮 太适合我了 😂",
        ],
        "responding_to_question": [
            "我知道我知道！是这样的...",
            "哈哈 这个我刚好了解一点",
            "等我想想... 好像是这样的",
            "我记得有人说过 应该是...",
            "你可以试试这样 我之前也遇到过",
            "这个我问过了 答案是...",
            "我也不太确定 但我猜...",
            "我帮你问问别的群 稍等 😂",
            "根据我的不靠谱记忆 应该是...",
            "你搜一下应该有 我之前看到过",
        ],
        "daily_chat": [
            "今天好累 但是群里这么热闹 又精神了 😂",
            "明天要上班 好想请假",
            "刚做完饭 今天的成品不错 👏",
            "有什么好看的电影推荐 周末看",
            "你们用什么手机壳 我想换个新的",
            "空调开太低了 冷死了",
            "今天被老板夸了 心情贼好 😂",
            "谁有好的壁纸 分享一下",
            "夜宵吃什么 在线等 急 🍜",
            "好困 但是舍不得睡 再聊会儿",
        ],
    },
    "en": {
        "general": [
            "Good morning everyone! It's Friday, almost off work 🔥🔥",
            "Lmaooo this meme is too good, gotta share",
            "Just had ramen, happiness level maxed out ❤️",
            "Anyone else bored of this sideways market? 😂",
            "Missed the big prize by one second, I'm dead 💀",
            "Did y'all see that news yesterday? Hilarious",
            "Weather is amazing today, don't even wanna go back inside",
            "What's everyone up to? Group is so quiet",
            "Just made tea, vibing, this is the life",
            "Anyone else browsing chats during work? 🤣",
            "That joke was so bad but I still laughed 😂",
            "Whose cat is that cute?! I want one too",
            "Rainy days are for staying in bed scrolling",
            "What time do you get off? I'm out at 5 sharp",
            "Feeling good today, virtual boba for everyone 🧋",
            "Someone said I talk too much, I think chats are for chatting 😤",
            "Any weekend plans? So bored",
            "Just saw a meme, can't stop laughing 😂",
            "What shows are you watching? Recommend something",
            "Craving hotpot so bad, anyone down? 🍲",
        ],
        "asking_recommendation": [
            "Played that button game my friend recommended till 3am... 💀",
            "Lemme tell you something funny, I missed the big prize by one second 💀",
            "Anyone wanna play that countdown thing? Boring alone",
            "You guys know that button game? My friend won and seemed pretty hyped",
            "Found a button game last night, surprisingly addictive",
            "There's a game perfect for playing in groups, we should try it",
            "Someone on my feed shared screenshots of that button game, looks fun",
            "Found a game we can all play together, just pressing a button lol",
            "Anyone tried The Button? My friend won't stop recommending it",
            "Found a casual game, zero brain required, just press a button, perfect for me 😂",
        ],
        "responding_to_question": [
            "I know I know! It's like this...",
            "Haha I actually know a bit about this",
            "Let me think... I think it's like this",
            "I remember someone saying, should be...",
            "You could try this, happened to me before",
            "I asked about this, the answer is...",
            "Not sure either, but my guess is...",
            "Let me ask another group, brb 😂",
            "Based on my unreliable memory, should be...",
            "Search for it, I've seen it before",
        ],
        "daily_chat": [
            "So tired today, but the group chat revived me 😂",
            "Gotta work tomorrow, wish I could call in sick",
            "Just cooked, today's dish turned out great 👏",
            "Any good movie recs for the weekend?",
            "What phone case are y'all using? I want a new one",
            "AC too cold, freezing here",
            "Got praised by my boss today, feeling great 😂",
            "Anyone got good wallpapers? Share pls",
            "Late night snack options? Need help asap 🍜",
            "So sleepy but don't wanna sleep yet, let's chat more",
        ],
    },
}

# Persona replies registry
_PERSONA_REPLIES: dict[str, dict[str, dict[str, list[str]]]] = {
    "crypto_veteran": _CRYPTO_VETERAN_REPLIES,
    "game_newbie": _GAME_NEWBIE_REPLIES,
    "airdrop_hunter": _AIRDROP_HUNTER_REPLIES,
    "data_analyst": _DATA_ANALYST_REPLIES,
    "community_active": _COMMUNITY_ACTIVE_REPLIES,
}

# Context keyword mapping -- maps detected keywords to context categories
_CONTEXT_KEYWORDS: dict[str, list[str]] = {
    "market_discussion": [
        "btc", "eth", "行情", "涨", "跌", "抄底", "高位", "牛市", "熊市",
        "market", "pump", "dump", "bullish", "bearish", "price", "chart",
        "仓位", "持仓", "合约", "现货", "k线", "macd", "rsi",
    ],
    "asking_recommendation": [
        "推荐", "有什么好", "什么好玩", "recommend", "suggest", "try",
        "值得", "worth", "最近玩什么", "有什么新", "project", "项目",
    ],
    "responding_to_question": [
        "?", "？", "怎么", "为什么", "how", "why", "what", "是什么",
        "能不能", "可以", "请问", "有人知道", "anyone know",
    ],
    "gaming_discussion": [
        "游戏", "原神", "王者", "game", "genshin", "play", "抽卡",
        "steam", "手游", "mobile game", "pvp", "pve", "氪金",
    ],
    "airdrop_discussion": [
        "空投", "airdrop", "撸毛", "交互", "interact", "farming",
        "mint", "snapshot", "快照", "白名单", "whitelist",
    ],
    "daily_chat": [
        "早上好", "gm", "晚安", "吃饭", "下班", "morning", "night",
        "lunch", "dinner", "weekend", "周末", "天气", "weather",
        "coffee", "咖啡",
    ],
}


# ============================================================================
# 2. Battle report templates
# ============================================================================

_ROOM_EMOJIS = {
    "coin": "🪙", "fast": "⚡", "standard": "🎯", "premium": "💎",
}
_ROOM_NAMES_ZH = {
    "coin": "金币房", "fast": "快速房", "standard": "标准房", "premium": "高级房",
}
_ROOM_NAMES_EN = {
    "coin": "Coin Room", "fast": "Fast Room", "standard": "Standard Room", "premium": "Premium Room",
}

_BATTLE_REPORT_TEMPLATES_ZH: list[str] = [
    """🔴 The Button Round #{round_id} 结束！
{room_emoji} {room_name}

💰 奖池总额: {prize_pool}
🏆 Final Hit: {winner} 赢得 {final_prize}
📊 总点击: {total_clicks}次
⏱️ 持续时间: {duration}

💡 {highlight}""",

    """🎯 Round #{round_id} | {room_emoji} {room_name}
━━━━━━━━━━━━━
奖池 {prize_pool} | 总点击 {total_clicks}次
🥇 最后一击: {winner} → {final_prize}
⏱️ {duration}
{highlight}""",

    """⚡ {room_name} #{round_id} 战报
{winner} 拿下 Final Hit！{final_prize} 到手 🎉
全场 {total_clicks} 次点击，持续 {duration}
总奖池 {prize_pool}
{highlight}""",

    """💥 Round #{round_id} 落幕 | {room_emoji} {room_name}
这一轮 {total_clicks} 人次参与，持续了 {duration}
{winner} 在最后时刻按下按钮，拿走了 {final_prize}
奖池总额: {prize_pool}
{highlight}""",

    """🔔 战报 | {room_name} #{round_id}
{total_clicks}次点击 · {duration} · 奖池{prize_pool}
🏆 {winner} 赢得 Final Hit {final_prize}
{highlight}""",

    """📊 {room_emoji} {room_name} Round #{round_id} 结算
参与人次: {total_clicks} | 持续: {duration}
最终赢家: {winner} ({final_prize})
奖池: {prize_pool}
{highlight}""",

    """🎊 {room_name} #{round_id} 完结撒花！
{winner} 成为最后的赢家，斩获 {final_prize}！
{total_clicks}次点击，{duration}的激烈博弈
{highlight}""",
]

_BATTLE_REPORT_TEMPLATES_EN: list[str] = [
    """🔴 The Button Round #{round_id} ended!
{room_emoji} {room_name}

💰 Prize Pool: {prize_pool}
🏆 Final Hit: {winner} won {final_prize}
📊 Total Clicks: {total_clicks}
⏱️ Duration: {duration}

💡 {highlight}""",

    """🎯 Round #{round_id} | {room_emoji} {room_name}
━━━━━━━━━━━━━
Pool {prize_pool} | Clicks {total_clicks}
🥇 Final Hit: {winner} → {final_prize}
⏱️ {duration}
{highlight}""",

    """⚡ {room_name} #{round_id} Report
{winner} claimed Final Hit! {final_prize} secured 🎉
{total_clicks} total clicks, lasted {duration}
Total pool: {prize_pool}
{highlight}""",

    """💥 Round #{round_id} Over | {room_emoji} {room_name}
{total_clicks} clicks over {duration}
{winner} pressed at the last moment, taking home {final_prize}
Total pool: {prize_pool}
{highlight}""",

    """🔔 Report | {room_name} #{round_id}
{total_clicks} clicks · {duration} · Pool {prize_pool}
🏆 {winner} wins Final Hit {final_prize}
{highlight}""",

    """📊 {room_emoji} {room_name} Round #{round_id} Settlement
Participants: {total_clicks} | Duration: {duration}
Winner: {winner} ({final_prize})
Pool: {prize_pool}
{highlight}""",

    """🎊 {room_name} #{round_id} Complete!
{winner} emerges as the final winner, claiming {final_prize}!
{total_clicks} clicks across {duration} of intense gameplay
{highlight}""",
]

_HIGHLIGHTS_ZH: list[str] = [
    "最后10秒连续5人点击，场面紧张刺激！",
    "Stage 5 阶段持续了足足2分钟才分出胜负",
    "分红已自动发放到所有参与者账户",
    "下一轮即将开始，奖池已有种子基金",
    "本轮最后阶段价格飙升至起始的10倍！",
    "全场最激烈的30秒诞生在Stage 4→5的转折点",
    "本轮分红金额创下新高 🔥",
    "赢家在最后1秒才出手 真沉得住气！",
    "参与人数比上一轮多了30%",
    "这轮的博弈策略十分精彩 值得复盘",
]

_HIGHLIGHTS_EN: list[str] = [
    "5 clicks in the last 10 seconds, intense!",
    "Stage 5 lasted 2 full minutes before settling",
    "Dividends auto-distributed to all participants",
    "Next round starting soon, seed pool ready",
    "Final stage price surged to 10x the starting price!",
    "Most intense 30 seconds at the Stage 4→5 transition",
    "This round's dividends hit a new high 🔥",
    "Winner struck in the very last second, nerves of steel!",
    "30% more participants than last round",
    "Brilliant strategy plays this round, worth reviewing",
]


# ============================================================================
# 3. Win story templates
# ============================================================================

_WIN_STORY_TEMPLATES_ZH: list[str] = [
    "兄弟们 {room_name}刚才最后{seconds}秒我手一抖点了一下 结果中了{amount} 😂 运气来了挡不住",
    "今天{room_name}分红到账了 {dividend} 虽然不多但胜在稳定",
    "我靠 刚才{room_name}最后关头我点了一下 居然是Final Hit！{amount}到手 😱",
    "跟大家报告一下 {room_name}这轮我拿了Middle Hit {amount} 虽然不是最大的但也够吃顿好的了",
    "哈哈哈 刚在{room_name}手滑按了一下 没想到是最后一击 {amount} 💰",
    "今天运气好 {room_name}分红分了{dividend} 加上之前的奖金 这周收益还行",
    "{room_name}打到Stage 5的时候我心态都崩了 结果最后{seconds}秒赌了一把 中了{amount}！",
    "分享一下 刚才{room_name}在3/4位置拿了{amount} 虽然不是大奖但也开心",
    "各位 我要去吃顿好的了 {room_name}刚赢了{amount} 请客请客 😂",
    "今天{room_name}分红{dividend}已到账 每天小赚一点 积少成多",
    "说出来你们可能不信 我第{click_number}次点击就是Final Hit {amount} 什么运气",
    "我朋友说让我试试{room_name} 没想到第一局就中了{amount} 新手运气太好了吧",
    "刚才{room_name}差点错过了 最后{seconds}秒才反应过来 结果居然赢了{amount}",
    "{room_name}今天这轮竞争好激烈 我在Stage 4进的 没想到能拿到1/4位置 {amount}",
    "终于轮到我了！{room_name}拿了一个位置奖 {amount} 虽然等了几轮但值了",
    "朋友们 金币房今天分红{dividend}金币 免费的钱为什么不赚",
]

_WIN_STORY_TEMPLATES_EN: list[str] = [
    "Bros, just fat-fingered a click in {room_name} with {seconds}s left, won {amount} 😂 Can't stop this luck",
    "Dividends from {room_name} just hit, {dividend}, not huge but consistent",
    "OMG just clicked in {room_name} at the last moment, it was the Final Hit! {amount} secured 😱",
    "Update: got Middle Hit in {room_name} this round, {amount}, not the jackpot but enough for a nice dinner",
    "Lmao accidentally clicked in {room_name}, turned out to be the last hit, {amount} 💰",
    "Lucky day, {room_name} dividends: {dividend}, plus earlier winnings, decent week",
    "When {room_name} hit Stage 5 I was losing it, then gambled in the last {seconds}s and won {amount}!",
    "Sharing: just got the 3/4 position in {room_name} for {amount}, not the big one but still happy",
    "Guys I'm treating myself tonight, just won {amount} in {room_name} 😂",
    "Today's {room_name} dividends: {dividend}, little gains add up",
    "You might not believe this but my click #{click_number} was the Final Hit, {amount}. What is this luck",
    "Friend told me to try {room_name}, won {amount} on my first round, beginner's luck is real",
    "Almost missed it in {room_name}, realized with {seconds}s left, ended up winning {amount}",
    "{room_name} was super competitive today, entered at Stage 4, somehow got the 1/4 position, {amount}",
    "Finally my turn! Got a position prize in {room_name}, {amount}, waited a few rounds but worth it",
    "Friends, Coin Room dividends today: {dividend} coins, why not take free money",
]


# ============================================================================
# 4. Meme / joke templates
# ============================================================================

_MEME_TEMPLATES_ZH: list[str] = [
    "当你在 Stage 5 还在犹豫要不要点的时候：\n🤡 ← 你\n⏰ 3...2...1...\n💀",
    "The Button 玩家的一天：\n6:00 起床\n6:01 看看Standard房倒计时\n6:02-23:59 刷新倒计时",
    "我：今天不玩了 好好休息\n手机通知：Stage 5 倒计时3秒\n我：👀",
    "钱包余额看了3遍才敢相信\n然后发现是金币不是U\n😂😂😂",
    "每次以为自己是Final Hit的时候\n总有个人在最后0.5秒冒出来\n你永远可以相信人类的手速",
    "The Button 的5个阶段：\n🌟 无所谓\n⚡ 有点心动\n💨 开始紧张\n🚀 手心出汗\n💥 心跳加速到怀疑人生",
    "对象问我在干嘛\n我：在工作\n实际上：盯着倒计时3...2...3...2...3...",
    "新手：这个游戏有什么策略吗\n老玩家：有 就是别问策略 直接点就完了\n新手：...那你为什么一直盯着不点\n老玩家：这就是策略",
    "金币房玩家：我不是来赚钱的 我就是来体验的\n金币房玩家（赢了之后）：有没有U的房间 我要玩真的",
    "差一秒就是Final Hit\n差一秒就是一顿火锅\n差一秒就是我和财富自由的距离\n💀💀💀",
    "我在群里推荐The Button\n群友：不信\n我：看我截图\n群友：修图的吧\n我：...\n群友：教教我怎么玩",
    "Stage 1：高高在上 爱理不理\nStage 5：拿起手机 仔细斟酌\n倒计时3秒：啊啊啊点点点！",
    "如果你觉得人生没有激情\n那你一定没在Stage 5最后3秒点过按钮\n心跳比坐过山车还刺激 💓",
    "普通玩家：我要理性分析 等到最佳时机\n实际操作：看到倒计时3秒 闭眼就是干",
    "The Button 教会我的人生道理：\n1. 犹豫就会败北\n2. 果断就会白给\n3. 玄学才是真理",
    "每天看群里有人晒战绩\n我：不信\n也每天看\n然后：算了试试\n最后：怎么这么上头",
]

_MEME_TEMPLATES_EN: list[str] = [
    "When you're hesitating at Stage 5:\n🤡 ← you\n⏰ 3...2...1...\n💀",
    "A Button player's day:\n6:00 Wake up\n6:01 Check Standard Room countdown\n6:02-23:59 Refresh countdown",
    "Me: Not playing today, need rest\nPhone notification: Stage 5 countdown 3s\nMe: 👀",
    "Checked wallet balance 3 times to be sure\nThen realized it's coins not tokens\n😂😂😂",
    "Every time you think you're the Final Hit\nSomeone pops up in the last 0.5s\nNever underestimate human reflexes",
    "The 5 stages of The Button:\n🌟 Whatever\n⚡ Kinda interested\n💨 Getting nervous\n🚀 Palms sweating\n💥 Heart rate through the roof",
    "Partner: What are you doing?\nMe: Working\nActually: Staring at countdown 3...2...3...2...3...",
    "Newbie: Any strategy for this game?\nVeteran: Yes. Don't ask about strategy, just click.\nNewbie: Then why are you just staring?\nVeteran: That IS the strategy.",
    "Coin Room player: I'm here for the experience, not money\nCoin Room player (after winning): Where's the real money room?",
    "One second from Final Hit\nOne second from a steak dinner\nOne second from financial freedom\n💀💀💀",
    "Me recommending The Button in chat\nFriend: Don't believe it\nMe: Look at my screenshot\nFriend: Photoshopped\nMe: ...\nFriend: So how do I play?",
    "Stage 1: Too cool to care\nStage 5: Picks up phone carefully\nCountdown 3s: SMASH THAT BUTTON!!!",
    "If you think life has no excitement\nYou haven't pressed the button at 3 seconds in Stage 5\nMore thrilling than a roller coaster 💓",
    "Rational player: I'll analyze and wait for the optimal moment\nActual gameplay: sees 3 seconds, closes eyes, clicks",
    "Life lessons from The Button:\n1. Hesitation leads to defeat\n2. Decisiveness leads to wasted money\n3. Vibes are the real strategy",
    "Every day seeing people flex wins in chat\nMe: Don't believe it\nAlso me: keeps watching\nThen: Let me just try\nFinally: Why is this so addictive",
]


# ============================================================================
# 5. The Button mention templates (for infiltration)
# ============================================================================

_MENTION_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "casual": {
        "zh": [
            "最近在玩一个叫 The Button 的东西 有人听说过吗",
            "昨晚无聊发现了一个按钮游戏 还挺上头的",
            "手机上偶然看到一个游戏 就一个按钮 按就完了",
            "哈 最近有个挺有意思的小游戏 名字就叫按钮",
            "你们有没有在刷到过一个叫 The Button 的 我朋友在玩",
            "闲着没事试了个新游戏 规则超级简单 就按一个按钮",
            "我同事一直在手机上按一个红色按钮 问了才知道是个游戏",
            "昨天刷TG看到有人在讨论一个按钮实验 有点好奇",
            "突然想起来 有人提过一个倒计时按钮的游戏 有玩的吗",
            "这两天在试一个Web3小游戏 叫什么来着 好像是 The Button",
            "看到群里有人提过一个按钮游戏 了解了一下觉得还行",
            "最近发现TG上有个按钮游戏挺火的 有人知道吗",
            "朋友安利了一个游戏给我 说是按按钮就行 我半信半疑",
            "上班摸鱼的时候发现了个有意思的 一个红色大按钮",
            "有个游戏名字叫 The Button 昨天试了下 挺简单的",
        ],
        "en": [
            "Anyone heard of this thing called The Button?",
            "Found a button game last night while bored, surprisingly addictive",
            "Stumbled onto a game on my phone, literally just one button",
            "Hah, there's this interesting little game lately, just called The Button",
            "Any of you seen The Button? My friend's been playing it",
            "Tried a new game out of boredom, rules are super simple, just one button",
            "My coworker keeps tapping a red button on their phone, turns out it's a game",
            "Saw people discussing a button experiment on TG yesterday, curious",
            "Just remembered, someone mentioned a countdown button game, anyone play it?",
            "Been trying a Web3 game these past days, think it's called The Button",
            "Saw someone in chat mention a button game, checked it out, seems decent",
            "Noticed a button game getting popular on TG, anyone know about it?",
            "Friend recommended a game, says just press a button, I'm skeptical",
            "Discovered something fun while slacking off at work, a big red button",
            "There's a game called The Button, tried it yesterday, pretty simple",
        ],
    },
    "experience": {
        "zh": [
            "跟大家说个事 前两天试了个按钮游戏 金币房免费 居然真的分到了分红",
            "有没有人玩过 The Button？我在金币房试了几轮觉得还不错",
            "分享一下 最近在玩一个倒计时按钮 免费的金币房还能分红 挺良心",
            "试了几天 The Button 说说感受 金币房零门槛 机制还挺透明的",
            "给大家推荐个我最近在玩的 就一个按钮 金币房免费 分红即时到账",
            "上周朋友推荐的那个按钮游戏 我玩了几天 金币房确实不花钱",
            "聊聊我的体验吧 The Button 金币房玩了大概10轮 分红是真的有",
            "不得不说 那个按钮游戏比我想象的好玩 倒计时的时候真的紧张",
            "试了下 The Button 的标准房 小赚了一点 但更多是刺激感",
            "玩了一周 The Button 的体验分享 金币房用来练手不错 免费的嘛",
            "The Button 我大概玩了有一周了 简单说下 金币房真的免费 分红也是真的",
            "最近每天都会打开 The Button 玩两轮 金币房 不花钱 就图个乐",
            "说实话一开始我是不信的 后来试了金币房 发现是真免费真分红",
            "在 The Button 金币房积累了点经验 Stage 3-4进场体验最好",
            "跟几个朋友一起试了 The Button 金币房PK 还挺好玩的",
        ],
        "en": [
            "So I tried this button game, coin room is free, and actually got dividends",
            "Anyone played The Button? Tried the Coin Room for a few rounds, pretty decent",
            "Sharing my experience: been playing a countdown button game, free coin room pays dividends, legit",
            "Tried The Button for a few days, here's my take: Coin Room is zero barrier, mechanics are transparent",
            "Recommending something I've been playing, just one button, Coin Room is free, instant dividends",
            "That button game my friend recommended, played for days, Coin Room is truly free",
            "Let me share: played The Button Coin Room about 10 rounds, dividends are real",
            "Gotta say, the button game is more fun than expected, countdown gets tense",
            "Tried The Button Standard Room, made a little, but mostly for the thrill",
            "One week of The Button, sharing: Coin Room is great for practice, it's free",
            "Been playing The Button for about a week, TLDR: Coin Room is free, dividends are real",
            "Playing The Button daily now, Coin Room, free, just for fun",
            "Honestly didn't believe it at first, tried Coin Room, actually free, actually pays dividends",
            "Gained some experience in The Button Coin Room, Stage 3-4 entry feels best",
            "Tried The Button Coin Room with friends, competing against each other was fun",
        ],
    },
    "ask_help": {
        "zh": [
            "有玩过 The Button 的吗？想问下 Premium 房的策略",
            "请教一下群里的大佬 The Button 的 Standard 房什么阶段进场比较好",
            "有没有 The Button 的老玩家 Stage 4 和 Stage 5 的策略有什么区别",
            "想问问大家 The Button 的分红是怎么算的 按点击数平分？",
            "请问 The Button 金币房转U房有什么建议吗 新手一个",
            "大佬们 The Button 的单调递增定价具体是怎么个递增法",
            "有人能解释下 The Button 的4个位置奖分别是怎么确定的吗",
            "问一下 The Button 不同房间的入场价格差多少",
            "想了解下 The Button 的经济模型 有懂的吗",
            "请问 The Button 是优先抢跑还是等待比较好",
            "求助 The Button 高级房的定价曲线有人研究过吗",
            "有人知道 The Button 的分红池比例是多少吗",
            "The Button 的阶段切换是根据什么来的 时间吗",
            "新手求问 The Button 金币房跟U房的区别大吗",
            "有没有大佬分析过 The Button 什么时候进场EV最高",
        ],
        "en": [
            "Anyone played The Button? Looking for Premium Room strategy tips",
            "Asking the experts, what stage is best to enter The Button Standard Room?",
            "Any Button veterans here? What's the strategy difference between Stage 4 and 5?",
            "Question: how are The Button dividends calculated? Split by click count?",
            "Any tips for transitioning from Coin Room to U-token rooms in The Button?",
            "Can someone explain The Button's monotonic pricing mechanism?",
            "Could anyone explain how the 4 position prizes work in The Button?",
            "What's the price difference between The Button rooms?",
            "Want to understand The Button's economic model, anyone knowledgeable?",
            "Is it better to front-run or wait in The Button?",
            "Has anyone studied The Button's Premium Room pricing curve?",
            "Does anyone know The Button's dividend pool percentage?",
            "What triggers stage transitions in The Button? Time-based?",
            "Newbie question: big difference between Coin Room and U rooms?",
            "Any analysis on when EV is highest for entering The Button?",
        ],
    },
    "data": {
        "zh": [
            "分析了一个新项目的分红模型 30%分红池按点击数平分 数学上挺有意思",
            "研究了一个博弈论实验的经济模型 EV分析结果还挺正",
            "做了个项目的定价曲线分析 分段线性+指数 能防刷量",
            "算了个新项目的纳什均衡 单调递增定价下的最优策略是等待",
            "最近分析了一个按钮游戏的代币经济学 30%即时分红+25% Final Hit 模型可持续",
            "对比了几个GameFi项目的经济模型 有一个30%即时分红的设计比较少见",
            "做了蒙特卡洛模拟 在合理参数下 某个按钮游戏的参与者EV为正",
            "从机制设计角度看了个项目 激励兼容性做得不错 玩家利益和平台利益对齐",
            "分析了一个5阶段倒计时的博弈 每个阶段的最优策略不一样 有意思",
            "发现一个项目的经济模型 8%种子基金循环机制保证了长期可玩性",
            "研究了一下某个按钮游戏的定价公式 指数阶段的增长率控制得还行",
            "从信息不对称的角度分析了一个项目 后入场者有信息优势但价格更高 有趣的权衡",
            "用博弈论框架分析了一下 这个按钮实验其实是个序贯博弈 不是同时博弈",
            "算了下某个项目的方差 高级房风险更高但期望收益也更好",
            "做了个简单的回归分析 参与人数和奖池规模有明显正相关",
        ],
        "en": [
            "Analyzed a new project's dividend model, 30% pool split by clicks, mathematically interesting",
            "Studied a game theory experiment's economic model, EV analysis checks out",
            "Did a pricing curve analysis, piecewise linear + exponential, prevents botting",
            "Calculated Nash equilibrium for a new project, optimal strategy under monotonic pricing is waiting",
            "Recently analyzed a button game's tokenomics, 30% instant dividend + 25% Final Hit, sustainable model",
            "Compared economic models across GameFi projects, one with 30% instant dividend is rare",
            "Ran Monte Carlo simulation, under reasonable parameters, participants have positive EV",
            "From mechanism design perspective, incentive compatibility is solid, player and platform interests aligned",
            "Analyzed a 5-stage countdown game theory, different optimal strategies per stage, fascinating",
            "Found a project with 8% seed fund recycling that ensures long-term playability",
            "Studied the pricing formula of a button game, exponential phase growth rate is reasonable",
            "From info asymmetry angle, late entrants have info advantage but higher price, interesting tradeoff",
            "Game theory framework analysis: this button experiment is sequential, not simultaneous game",
            "Calculated variance for a project, premium room higher risk but better expected returns",
            "Simple regression: strong positive correlation between participant count and pool size",
        ],
    },
    "screenshot": {
        "zh": [
            "兄弟们 这波血赚 😂 [金币房分红截图]",
            "刚才 Standard Room 最后3秒 差点心脏骤停 结果真中了",
            "看看我的分红记录 虽然单次不多但每轮都有 [截图]",
            "不是吧 我居然是 Final Hit！[战绩截图]",
            "给你们看看我金币房的成绩 还可以吧 😂 [截图]",
            "这运气 我自己都不信 [中奖截图]",
            "分红到账截图 今天赚了一杯咖啡钱 ☕ [截图]",
            "快看快看！Standard Room 这轮我是最后一击！[截图]",
            "金币房的分红 免费的羊毛为什么不薅 [截图]",
            "今天的战绩分享 小赚一笔 心情不错 [截图]",
            "有图有真相 The Button 分红是真的 [截图]",
            "我在高级房赢了个位置奖 开心 [截图]",
            "看我的连续分红记录 每一轮都参与了 [截图]",
            "Stage 5 最后时刻的截图 心跳加速 [截图]",
            "金币房今天的收益 免费体验yyds [截图]",
        ],
        "en": [
            "Bros look at this profit 😂 [Coin Room dividend screenshot]",
            "Standard Room last 3 seconds, nearly had a heart attack, actually won",
            "Check my dividend history, small per round but every round counts [screenshot]",
            "No way I'm the Final Hit! [results screenshot]",
            "Here's my Coin Room results, not bad right? 😂 [screenshot]",
            "This luck, even I don't believe it [winning screenshot]",
            "Dividend deposit screenshot, earned a coffee today ☕ [screenshot]",
            "Look look! Standard Room I'm the last hitter this round! [screenshot]",
            "Coin Room dividends, why not take free money [screenshot]",
            "Today's results, small win, good mood [screenshot]",
            "Proof with pics, The Button dividends are real [screenshot]",
            "Won a position prize in Premium Room, happy [screenshot]",
            "Check my continuous dividend record, participated every round [screenshot]",
            "Screenshot of the Stage 5 final moment, heart racing [screenshot]",
            "Coin Room earnings today, free experience is the best [screenshot]",
        ],
    },
}


# ============================================================================
# 6. Synonym / expression variant tables
# ============================================================================

_GAME_LINK = "https://t.me/rwans_the_button_bot/game"

# ============================================================================
# 6a. Promo-with-link template pools  (5 approaches x 4 languages)
#     ~30% of templates contain the link; ~70% mention name only
# ============================================================================

_PROMO_WITH_LINK: dict[str, dict[str, list[str]]] = {
    "casual_mention": {
        "zh": [
            # --- with link ---
            "最近发现个挺有意思的社会实验 叫 The Button，按一下按钮就行 金币房不花钱 " + _GAME_LINK,
            "无聊刷TG看到一个按钮游戏 挺好玩的 " + _GAME_LINK + " 金币房免费",
            # --- no link ---
            "你们听说过 The Button 吗 最近好多人在聊",
            "刚看到有人在群里提那个按钮游戏 听说挺有意思的",
            "最近发现个叫 The Button 的东西 就按一个按钮 规则贼简单",
            "我同事一直在手机上玩一个什么按钮游戏 看着挺上头",
            "偶然刷到一个倒计时按钮的项目 有人了解不",
            "那个 The Button 你们玩过没 我朋友天天跟我安利",
        ],
        "en": [
            # --- with link ---
            "stumbled on this social experiment thing called The Button, coin room is free " + _GAME_LINK,
            "found this button game on tg ngl its kinda fun " + _GAME_LINK,
            # --- no link ---
            "anyone heard of The Button? been seeing it everywhere lately",
            "my friend keeps telling me about this button game, sounds interesting",
            "saw people talking about The Button in another chat, anyone tried it?",
            "there's this countdown button game going around, super simple concept",
            "someone in my group mentioned The Button yesterday, anyone know what it is?",
            "ngl kinda curious about that button game people keep bringing up",
        ],
        "ru": [
            # --- with link ---
            "нашёл интересную штуку The Button, монетная комната бесплатно " + _GAME_LINK,
            "кто-нибудь видел эту игру с кнопкой? бесплатная комната есть " + _GAME_LINK,
            # --- no link ---
            "слышали про The Button? народ обсуждает",
            "друг рассказал про какую-то игру с кнопкой, кто в курсе?",
            "в другом чате обсуждают The Button, кто пробовал?",
            "есть такая игра The Button, просто жмёшь кнопку, правила простые",
            "наткнулся на проект с кнопкой в тг, интересно выглядит",
        ],
        "vi": [
            # --- with link ---
            "mới thấy cái game The Button trên tg, phòng coin miễn phí " + _GAME_LINK,
            "ai biết The Button không? chơi free " + _GAME_LINK,
            # --- no link ---
            "nghe nói về The Button chưa? dạo này nhiều người bàn lắm",
            "bạn bè cứ kêu chơi cái game nút bấm gì đó, ai biết không?",
            "thấy mọi người nói về The Button, luật chơi đơn giản lắm",
            "mới phát hiện cái game bấm nút, đơn giản mà hấp dẫn",
            "có ai chơi The Button chưa? nghe bảo hay lắm",
        ],
    },
    "experience_share": {
        "zh": [
            # --- with link ---
            "试了一下 The Button 金币房不花钱 分红还是真的 " + _GAME_LINK,
            "玩了几天 The Button 体验还不错 给你们链接 " + _GAME_LINK,
            # --- no link ---
            "ngl 最近在玩那个按钮游戏 金币房免费 分红模型还挺公平的",
            "跟大家说下 The Button 金币房试了几轮 分红真到账了",
            "那个按钮游戏我玩了一周了 金币房零成本 每轮都有分红",
            "分享一下 The Button 的体验 倒计时到 Stage 5 的时候真的紧张",
            "试了 The Button 标准房 小赚了点 主要是刺激感不错",
            "The Button 金币房确实免费 我已经玩了好几轮了 分红是即时的",
        ],
        "en": [
            # --- with link ---
            "been playing The Button for a week now, coin room is legit free " + _GAME_LINK,
            "tried The Button, dividends are instant and real " + _GAME_LINK,
            # --- no link ---
            "ngl been playing this button game lately, the coin room is free and the dividend model is actually kinda fair",
            "The Button coin room is actually free, tried a few rounds, dividends hit my account instantly",
            "sharing my experience with The Button, stage 5 countdown is genuinely intense",
            "played The Button standard room, made a little, mostly fun tho",
            "the button game is surprisingly addictive, coin room costs nothing",
            "so I tried The Button and the 30% dividend pool is real, not bad at all",
        ],
        "ru": [
            # --- with link ---
            "попробовал The Button, дивиденды реальные и моментальные " + _GAME_LINK,
            "играю в The Button уже неделю, монетная комната бесплатно " + _GAME_LINK,
            # --- no link ---
            "поиграл в The Button, монетная комната реально бесплатная",
            "делюсь опытом: The Button, дивиденды 30% приходят сразу",
            "кнопочная игра на самом деле затягивает, этап 5 — нервы на пределе",
            "попробовал стандартную комнату The Button, немного заработал",
            "The Button монетная комната без вложений, играю каждый день",
        ],
        "vi": [
            # --- with link ---
            "chơi The Button được tuần rồi, phòng coin free thật " + _GAME_LINK,
            "thử The Button, cổ tức về liền " + _GAME_LINK,
            # --- no link ---
            "chơi The Button rồi, phòng coin miễn phí, chia cổ tức thật",
            "chia sẻ trải nghiệm The Button, stage 5 hồi hộp cực",
            "cái game bấm nút này gây nghiện thật, phòng coin không tốn gì",
            "thử phòng tiêu chuẩn The Button, kiếm được chút, chủ yếu vui",
            "The Button cổ tức 30% về tức thì, khá ổn",
        ],
    },
    "ask_for_help": {
        "zh": [
            # --- with link ---
            "有没有人玩过 The Button？想问下策略 " + _GAME_LINK,
            "求助 The Button 标准房什么阶段进场好 " + _GAME_LINK,
            # --- no link ---
            "请问有人玩 The Button 吗 分红是怎么算的",
            "那个按钮游戏的 Stage 4 和 Stage 5 策略有啥区别",
            "有 The Button 的老玩家吗 金币房转U房有什么建议",
            "想了解下 The Button 的经济模型 有懂的吗",
            "问一下 The Button 是等到后面再按好还是早点按好",
            "The Button 的分红池比例是多少啊 有人知道吗",
        ],
        "en": [
            # --- with link ---
            "anyone played The Button? need some strategy tips " + _GAME_LINK,
            "looking for advice on The Button standard room " + _GAME_LINK,
            # --- no link ---
            "has anyone tried The Button? how do the dividends work exactly?",
            "what's the difference between stage 4 and stage 5 strategy in The Button?",
            "any Button veterans here? tips for transitioning from coin room to real rooms?",
            "trying to understand The Button's economic model, anyone can explain?",
            "is it better to click early or wait in The Button?",
            "does anyone know The Button's dividend pool percentage?",
        ],
        "ru": [
            # --- with link ---
            "Кто-нибудь пробовал The Button? Там бесплатная комната, дивиденды 30% мгновенно " + _GAME_LINK,
            "нужна помощь с The Button, какой этап лучше для входа " + _GAME_LINK,
            # --- no link ---
            "кто играл в The Button? как считаются дивиденды?",
            "в чём разница стратегии на этапе 4 и 5 в The Button?",
            "есть опытные игроки The Button? советы по переходу из монетной комнаты?",
            "кто разбирается в экономике The Button?",
            "лучше жать рано или ждать в The Button?",
        ],
        "vi": [
            # --- with link ---
            "ai chơi The Button rồi cho mình hỏi chiến thuật " + _GAME_LINK,
            "cần tư vấn về phòng tiêu chuẩn The Button " + _GAME_LINK,
            # --- no link ---
            "ai chơi The Button chưa? cổ tức tính sao vậy?",
            "stage 4 với stage 5 The Button khác nhau thế nào?",
            "có ai chơi lâu The Button không? mình mới bắt đầu",
            "ai hiểu mô hình kinh tế The Button giải thích giúp?",
            "The Button nên bấm sớm hay đợi?",
        ],
    },
    "data_analysis": {
        "zh": [
            # --- with link ---
            "分析了一个新项目的分红模型 30%即时分配 数学上可持续 " + _GAME_LINK,
            "做了个 The Button 的 EV 计算 参数合理时期望为正 " + _GAME_LINK,
            # --- no link ---
            "研究了一个博弈论实验的经济模型 30%分红+25% Final Hit 模型挺健康的",
            "分析了一个按钮游戏的定价曲线 分段线性+指数 能有效防刷",
            "算了个项目的纳什均衡 单调递增定价下最优策略是等待",
            "跑了蒙特卡洛模拟 某个按钮游戏在合理参数下参与者EV为正",
            "对比了几个 GameFi 的经济模型 有一个30%即时分红的设计比较少见",
            "从机制设计角度看了个项目 激励兼容性不错 玩家利益和平台对齐",
        ],
        "en": [
            # --- with link ---
            "analyzed a new project's dividend model, 30% instant payout, math checks out " + _GAME_LINK,
            "ran EV calculations on The Button, positive under reasonable params " + _GAME_LINK,
            # --- no link ---
            "studied a game theory experiment, 30% dividend + 25% final hit model is sustainable",
            "pricing curve analysis on a button game: piecewise linear + exponential, anti-bot",
            "calculated nash equilibrium for a project, optimal strategy under monotonic pricing is waiting",
            "monte carlo sim shows positive EV for participants under reasonable parameters",
            "compared gamefi economic models, one with 30% instant dividend is rare",
            "mechanism design perspective: incentive compatibility is solid, interests aligned",
        ],
        "ru": [
            # --- with link ---
            "проанализировал модель дивидендов, 30% мгновенно, математика сходится " + _GAME_LINK,
            "посчитал EV для The Button, положительный при нормальных параметрах " + _GAME_LINK,
            # --- no link ---
            "исследовал экономическую модель, 30% дивиденды + 25% финальный удар, устойчиво",
            "кривая цен: линейная + экспонента, защита от ботов",
            "равновесие Нэша: оптимальная стратегия при растущей цене — ждать",
            "монте-карло показывает положительный EV для участников",
            "сравнил модели gamefi, 30% мгновенных дивидендов — редкость",
        ],
        "vi": [
            # --- with link ---
            "phân tích mô hình cổ tức, 30% trả ngay, toán học hợp lý " + _GAME_LINK,
            "tính EV cho The Button, dương với tham số hợp lý " + _GAME_LINK,
            # --- no link ---
            "nghiên cứu mô hình kinh tế, 30% cổ tức + 25% final hit, bền vững",
            "đường cong giá: tuyến tính + mũ, chống bot hiệu quả",
            "cân bằng Nash: chiến lược tối ưu với giá tăng đơn điệu là chờ",
            "monte carlo cho thấy EV dương cho người chơi",
            "so sánh các mô hình gamefi, 30% cổ tức tức thì khá hiếm",
        ],
    },
    "screenshot_share": {
        "zh": [
            # --- with link ---
            "刚才在快速房赢了 差点心脏停了 😂 " + _GAME_LINK,
            "金币房分红到账截图 免费的为什么不玩 " + _GAME_LINK,
            # --- no link ---
            "兄弟们看看这波 金币房分红又到了 [截图]",
            "标准房最后3秒我居然是 Final Hit 不敢相信 [截图]",
            "今天 The Button 分红截图 虽然不多但每轮都有",
            "刚在快速房赢了个位置奖 运气爆棚 😂 [截图]",
            "The Button 金币房今天的收益 白嫖yyds [截图]",
            "看我的连续分红记录 每一轮都有 [截图]",
        ],
        "en": [
            # --- with link ---
            "just won in fast room, nearly had a heart attack 😂 " + _GAME_LINK,
            "coin room dividend proof, why not play when its free " + _GAME_LINK,
            # --- no link ---
            "bros check this out, coin room dividends again [screenshot]",
            "standard room last 3 seconds and I'm the final hit wtf [screenshot]",
            "The Button dividends today, small but every round [screenshot]",
            "won a position prize in fast room, luck is crazy 😂 [screenshot]",
            "The Button coin room earnings today, free money is free money [screenshot]",
            "look at my consecutive dividend record [screenshot]",
        ],
        "ru": [
            # --- with link ---
            "только что выиграл в быстрой комнате 😂 " + _GAME_LINK,
            "дивиденды монетной комнаты, бесплатно же " + _GAME_LINK,
            # --- no link ---
            "смотрите, опять дивиденды в монетной комнате [скриншот]",
            "стандартная комната, последние 3 секунды — я финальный удар [скриншот]",
            "дивиденды The Button сегодня, немного но стабильно",
            "выиграл позиционный приз в быстрой комнате 😂 [скриншот]",
            "заработок в монетной комнате The Button, халява [скриншот]",
        ],
        "vi": [
            # --- with link ---
            "vừa thắng ở phòng nhanh, tim đập loạn 😂 " + _GAME_LINK,
            "bằng chứng cổ tức phòng coin, miễn phí mà " + _GAME_LINK,
            # --- no link ---
            "ae xem, cổ tức phòng coin lại về [screenshot]",
            "phòng tiêu chuẩn 3 giây cuối mình là final hit [screenshot]",
            "cổ tức The Button hôm nay, ít nhưng vòng nào cũng có",
            "thắng giải vị trí ở phòng nhanh, may quá 😂 [screenshot]",
            "thu nhập phòng coin The Button hôm nay, free mà [screenshot]",
        ],
    },
}


# ============================================================================
# 6b. Link reply templates (when someone asks for the link)
# ============================================================================

_LINK_REPLY: dict[str, list[str]] = {
    "zh": [
        "在这儿 " + _GAME_LINK + " 金币房免费先体验下",
        "给你 " + _GAME_LINK + " 金币房不花钱 先试试",
        "链接 " + _GAME_LINK + " 注册送金币 直接玩金币房",
        _GAME_LINK + " 这个就是 进去就能玩 金币房零成本",
        "来 " + _GAME_LINK + " 先从金币房开始 不要钱的",
        _GAME_LINK + " 点进去就行 金币房免费 分红即时到账",
        "发你了 " + _GAME_LINK + " 金币房白嫖 不用充钱",
        "在这 " + _GAME_LINK + " 进去选金币房就行 免费的",
    ],
    "en": [
        "here you go fam " + _GAME_LINK + " the coin room is free",
        _GAME_LINK + " coin room costs nothing, just try it",
        "here " + _GAME_LINK + " start with coin room, zero cost",
        "link: " + _GAME_LINK + " free coins on signup, coin room is free",
        _GAME_LINK + " just hop in, coin room is free, dividends are instant",
        "got you " + _GAME_LINK + " coin room is the free one",
        "there ya go " + _GAME_LINK + " start with coin room no money needed",
        _GAME_LINK + " click and play, coin room is free to try",
    ],
    "ru": [
        "вот " + _GAME_LINK + " монетная комната бесплатно",
        _GAME_LINK + " заходи, монетная комната без вложений",
        "держи " + _GAME_LINK + " начни с монетной комнаты, бесплатно",
        "ссылка " + _GAME_LINK + " монетная комната бесплатная, дивиденды моментально",
        _GAME_LINK + " просто зайди, монетная комната бесплатно",
        "лови " + _GAME_LINK + " монетная комната без денег",
    ],
    "vi": [
        "đây nè " + _GAME_LINK + " phòng coin miễn phí",
        _GAME_LINK + " vào chơi phòng coin free luôn",
        "link đây " + _GAME_LINK + " phòng coin không tốn gì",
        _GAME_LINK + " bấm vào là chơi được, phòng coin miễn phí",
        "gửi bạn " + _GAME_LINK + " bắt đầu từ phòng coin, free",
        _GAME_LINK + " phòng coin miễn phí, cổ tức về liền",
    ],
}


_SYNONYM_MAP_ZH: dict[str, list[str]] = {
    "有意思": ["挺有趣", "蛮好玩", "值得一看", "挺特别的"],
    "不错": ["还行", "可以", "还可以", "挺好的"],
    "赚了": ["小赚", "到手了", "入账了", "收益了"],
    "好玩": ["上头", "有意思", "挺有趣", "还不错"],
    "免费": ["不花钱", "零成本", "白嫖", "不要钱"],
    "厉害": ["牛", "强", "绝了", "可以的"],
    "最近": ["这两天", "前几天", "这阵子", "近期"],
    "发现": ["看到", "找到", "注意到", "碰到"],
    "试试": ["体验下", "试一下", "玩玩", "看看"],
    "觉得": ["感觉", "认为", "我看", "个人觉得"],
    "分红": ["收益", "回报", "奖励", "分成"],
    "机制": ["模型", "设计", "规则", "玩法"],
    "参与": ["玩", "试", "加入", "体验"],
    "简单": ["容易", "不复杂", "上手快", "零门槛"],
    "项目": ["游戏", "东西", "产品", "平台"],
}

_SYNONYM_MAP_EN: dict[str, list[str]] = {
    "interesting": ["cool", "neat", "fascinating", "worth checking"],
    "good": ["decent", "solid", "not bad", "pretty nice"],
    "earned": ["made", "got", "won", "received"],
    "fun": ["addictive", "entertaining", "enjoyable", "engaging"],
    "free": ["zero cost", "no money needed", "costs nothing", "at no cost"],
    "great": ["amazing", "awesome", "impressive", "fantastic"],
    "recently": ["lately", "these days", "past few days", "this week"],
    "found": ["discovered", "came across", "stumbled on", "noticed"],
    "try": ["check out", "give it a go", "test", "experience"],
    "think": ["feel", "believe", "reckon", "figure"],
    "dividends": ["returns", "rewards", "payouts", "earnings"],
    "mechanism": ["model", "design", "system", "framework"],
    "participate": ["play", "join", "take part", "get involved"],
    "simple": ["easy", "straightforward", "intuitive", "no-brainer"],
    "project": ["game", "thing", "product", "platform"],
}

_EMOJI_VARIANTS: dict[str, list[str]] = {
    "😂": ["🤣", "哈哈", "lol", "😆"],
    "😱": ["😨", "天呐", "omg", "🫨"],
    "💀": ["☠️", "gg", "我人没了", "裂开"],
    "🔥": ["🫡", "💪", "nice", "✨"],
    "💰": ["🤑", "💵", "money", "💲"],
    "❤️": ["🥰", "♥️", "❣️", "💕"],
    "👀": ["🧐", "hmm", "看看", "👁️"],
    "😤": ["😠", "生气", "哼", "💢"],
    "🎉": ["🥳", "耶", "yay", "🎊"],
    "👏": ["👍", "nice", "不错", "666"],
}


# ============================================================================
# Main engine class
# ============================================================================

class TemplateContentEngine:
    """Template-based content generation engine that requires no AI API.

    Uses pre-written template pools with variable substitution and randomisation
    to produce natural-sounding messages. Tracks recently used templates to
    avoid short-term repetition.
    """

    def __init__(self, recent_history_size: int = 50) -> None:
        self._recent_templates: deque[str] = deque(maxlen=recent_history_size)
        self._recent_hashes: deque[str] = deque(maxlen=recent_history_size)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_template(self, pool: list[str], max_retries: int = 10) -> str:
        """Pick a random template, avoiding recently used ones."""
        for _ in range(max_retries):
            candidate = random.choice(pool)
            h = hashlib.md5(candidate.encode()).hexdigest()[:12]
            if h not in self._recent_hashes:
                self._recent_hashes.append(h)
                self._recent_templates.append(candidate)
                return candidate
        # Fallback: just pick randomly
        return random.choice(pool)

    @staticmethod
    def _detect_context(context: str) -> str:
        """Map a free-text context description to one of the known context categories."""
        ctx_lower = context.lower()
        best_cat = "general"
        best_score = 0
        for category, keywords in _CONTEXT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in ctx_lower)
            if score > best_score:
                best_score = score
                best_cat = category
        return best_cat

    @staticmethod
    def _is_chinese(text: str) -> bool:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk > len(text) * 0.1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_chat_response(
        self,
        persona_id: str,
        context: str,
        language: str = "zh",
    ) -> str:
        """Generate a chat reply based on persona and context.

        Args:
            persona_id: One of the 5 persona IDs.
            context: Free-text context description or recent chat snippet.
            language: "zh" or "en".

        Returns:
            A natural-sounding chat message string.
        """
        lang = language if language in ("zh", "en") else "zh"
        replies = _PERSONA_REPLIES.get(persona_id, _COMMUNITY_ACTIVE_REPLIES)
        lang_replies = replies.get(lang, replies.get("zh", {}))

        # Detect context category
        category = self._detect_context(context)

        # Try the detected category first, fall back to "general"
        pool = lang_replies.get(category, lang_replies.get("general", []))
        if not pool:
            pool = lang_replies.get("general", ["..."])

        return self._pick_template(pool)

    def generate_battle_report(
        self,
        round_data: dict[str, Any],
        language: str = "zh",
    ) -> str:
        """Generate a battle report from round data.

        Expected keys in round_data:
            round_id, room_type, prize_pool, winner, final_prize,
            total_clicks, duration.
        Optional: middle_winner, middle_prize, q1_winner, q1_prize,
                  q3_winner, q3_prize, highlight.
        """
        lang = language if language in ("zh", "en") else "zh"
        templates = _BATTLE_REPORT_TEMPLATES_ZH if lang == "zh" else _BATTLE_REPORT_TEMPLATES_EN
        highlights = _HIGHLIGHTS_ZH if lang == "zh" else _HIGHLIGHTS_EN

        room_type = round_data.get("room_type", "standard")
        room_names = _ROOM_NAMES_ZH if lang == "zh" else _ROOM_NAMES_EN

        params = {
            "round_id": round_data.get("round_id", "?"),
            "room_emoji": _ROOM_EMOJIS.get(room_type, "🎯"),
            "room_name": room_names.get(room_type, room_type),
            "prize_pool": round_data.get("prize_pool", "?"),
            "winner": round_data.get("winner", "???"),
            "final_prize": round_data.get("final_prize", "?"),
            "total_clicks": round_data.get("total_clicks", "?"),
            "duration": round_data.get("duration", "?"),
            "highlight": round_data.get("highlight", random.choice(highlights)),
        }

        template = self._pick_template(templates)
        return template.format(**params)

    def generate_win_story(
        self,
        win_data: dict[str, Any],
        language: str = "zh",
    ) -> str:
        """Generate a first-person winning story.

        Expected keys in win_data:
            room_type, amount.
        Optional: seconds, dividend, click_number, prize_type.
        """
        lang = language if language in ("zh", "en") else "zh"
        templates = _WIN_STORY_TEMPLATES_ZH if lang == "zh" else _WIN_STORY_TEMPLATES_EN

        room_type = win_data.get("room_type", "standard")
        room_names = _ROOM_NAMES_ZH if lang == "zh" else _ROOM_NAMES_EN
        currency = "金币" if room_type == "coin" and lang == "zh" else ("coins" if room_type == "coin" else "U")

        amount_val = win_data.get("amount", "???")
        amount_str = f"{amount_val}{currency}" if not str(amount_val).endswith(("U", "金币", "coins")) else str(amount_val)

        dividend_val = win_data.get("dividend", win_data.get("amount", "???"))
        dividend_str = f"{dividend_val}{currency}" if not str(dividend_val).endswith(("U", "金币", "coins")) else str(dividend_val)

        params = {
            "room_name": room_names.get(room_type, room_type),
            "amount": amount_str,
            "seconds": win_data.get("seconds", random.choice([1, 2, 3, 5])),
            "dividend": dividend_str,
            "click_number": win_data.get("click_number", random.randint(50, 500)),
        }

        # Filter templates that use keys we have data for, or fill with defaults
        template = self._pick_template(templates)

        try:
            return template.format(**params)
        except KeyError:
            # Fallback: just pick a simple one
            if lang == "zh":
                return f"刚在{params['room_name']}赢了{params['amount']} 运气不错 😂"
            return f"Just won {params['amount']} in {params['room_name']}, lucky day 😂"

    def generate_meme(self, topic: str = "", language: str = "zh") -> str:
        """Generate a meme / joke, optionally filtered by topic keywords.

        Args:
            topic: Optional topic hint (e.g. "stage5", "countdown", "dividends").
            language: "zh" or "en".
        """
        lang = language if language in ("zh", "en") else "zh"
        pool = _MEME_TEMPLATES_ZH if lang == "zh" else _MEME_TEMPLATES_EN

        if topic:
            topic_lower = topic.lower()
            filtered = [t for t in pool if topic_lower in t.lower()]
            if filtered:
                pool = filtered

        return self._pick_template(pool)

    def generate_thebutton_mention(
        self,
        approach: str,
        persona_id: str,
        language: str = "zh",
    ) -> str:
        """Generate a natural The Button mention for infiltration.

        Args:
            approach: One of "casual", "experience", "ask_help", "data", "screenshot".
            persona_id: Persona ID (used for tone adjustments).
            language: "zh" or "en".
        """
        lang = language if language in ("zh", "en") else "zh"

        approach_templates = _MENTION_TEMPLATES.get(approach, _MENTION_TEMPLATES["casual"])
        pool = approach_templates.get(lang, approach_templates.get("zh", []))

        if not pool:
            pool = _MENTION_TEMPLATES["casual"].get(lang, ["最近发现个有意思的游戏"])

        result = self._pick_template(pool)

        # Light persona-based post-processing
        if persona_id == "game_newbie":
            if lang == "zh" and not any(e in result for e in ["😂", "😱", "💀", "🔥", "！"]):
                result += random.choice([" 😂", " 😱", "！", " 🔥"])
        elif persona_id == "community_active":
            if lang == "zh" and not any(e in result for e in ["😂", "💀", "🔥", "❤️"]):
                result += random.choice([" 😂", " 💀", " 🔥", " 哈哈"])
        elif persona_id == "data_analyst":
            # More measured tone, strip heavy emojis
            result = re.sub(r"[😂🤣💀😱🔥]", "", result).strip()
        elif persona_id == "crypto_veteran":
            # Strip heavy emojis, keep minimal ones
            heavy = ["😂", "🤣", "💀", "😱", "🔥", "❤️", "😭"]
            for e in heavy:
                if e in result and random.random() < 0.7:
                    result = result.replace(e, "")
            result = result.strip()

        return result

    def generate_content_variants(
        self,
        content: str,
        count: int = 5,
    ) -> list[str]:
        """Generate multiple variants of a message via synonym replacement, emoji
        variation, and light word-order adjustments.

        Args:
            content: The base message to create variants of.
            count: Number of variants to produce.

        Returns:
            A list of up to *count* distinct variant strings.
        """
        is_zh = self._is_chinese(content)
        synonym_map = _SYNONYM_MAP_ZH if is_zh else _SYNONYM_MAP_EN
        variants: list[str] = []
        seen: set[str] = {content.strip()}

        for _ in range(count * 3):  # try extra times to get unique ones
            if len(variants) >= count:
                break
            variant = content

            # 1. Synonym replacement (1-3 replacements per variant)
            num_replacements = random.randint(1, 3)
            for _ in range(num_replacements):
                for original, synonyms in synonym_map.items():
                    if original in variant and random.random() < 0.5:
                        variant = variant.replace(original, random.choice(synonyms), 1)
                        break

            # 2. Emoji variation
            for emoji_orig, emoji_alts in _EMOJI_VARIANTS.items():
                if emoji_orig in variant and random.random() < 0.4:
                    variant = variant.replace(emoji_orig, random.choice(emoji_alts), 1)

            # 3. Light word-order adjustments for Chinese
            if is_zh and random.random() < 0.3:
                # Occasionally swap two short clauses separated by comma
                parts = re.split(r"[，,]", variant, maxsplit=1)
                if len(parts) == 2 and len(parts[0]) < 20 and len(parts[1]) < 20:
                    sep = "，" if "，" in variant else ","
                    variant = parts[1].strip() + sep + parts[0].strip()

            # 4. Occasional filler / trailing variation
            if random.random() < 0.2:
                if is_zh:
                    fillers = ["hh", "哈哈", "嘿嘿", "emmm", "~"]
                    variant = variant.rstrip() + " " + random.choice(fillers)
                else:
                    fillers = ["haha", "lol", "heh", "tbh", "ngl"]
                    variant = variant.rstrip() + " " + random.choice(fillers)

            # 5. Punctuation variation
            if random.random() < 0.25:
                if variant.endswith("。"):
                    variant = variant[:-1]
                elif variant.endswith(".") and not variant.endswith("..."):
                    variant = variant[:-1]
                elif not variant.endswith(("!", "！", "?", "？", ".", "。", "...")):
                    if random.random() < 0.5:
                        variant += "~" if is_zh else "!"

            stripped = variant.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                variants.append(stripped)

        # If we still don't have enough, pad with light modifications
        while len(variants) < count:
            base = content if not variants else random.choice(variants)
            padded = base.rstrip() + (" " + random.choice(["🤔", "👀", "💡", "~", "..."]))
            if padded.strip() not in seen:
                seen.add(padded.strip())
                variants.append(padded.strip())
            else:
                # Last resort: just add the original with minor tweak
                variants.append(base)
                break

        return variants[:count]

    def generate_promo_with_link(
        self,
        persona_id: str,
        approach: str,
        language: str = "zh",
    ) -> str:
        """Return a promo message that may or may not contain the game link.

        The template pools already have ~30% with link and ~70% without.

        Args:
            persona_id: Persona ID (used for light tone adjustments).
            approach: One of "casual_mention", "experience_share",
                      "ask_for_help", "data_analysis", "screenshot_share".
            language: "zh", "en", "ru", or "vi".

        Returns:
            A natural-sounding promo message string.
        """
        lang = language if language in ("zh", "en", "ru", "vi") else "zh"

        approach_pool = _PROMO_WITH_LINK.get(approach, _PROMO_WITH_LINK["casual_mention"])
        pool = approach_pool.get(lang, approach_pool.get("zh", []))

        if not pool:
            pool = _PROMO_WITH_LINK["casual_mention"].get(lang, ["最近发现个有意思的游戏"])

        result = self._pick_template(pool)

        # Light persona-based post-processing (same logic as generate_thebutton_mention)
        if persona_id == "game_newbie":
            if lang == "zh" and not any(e in result for e in ["😂", "😱", "💀", "🔥", "！"]):
                result += random.choice([" 😂", " 😱", "！", " 🔥"])
        elif persona_id == "community_active":
            if lang == "zh" and not any(e in result for e in ["😂", "💀", "🔥", "❤️"]):
                result += random.choice([" 😂", " 💀", " 🔥", " 哈哈"])
        elif persona_id == "data_analyst":
            result = re.sub(r"[😂🤣💀😱🔥]", "", result).strip()
        elif persona_id == "crypto_veteran":
            heavy = ["😂", "🤣", "💀", "😱", "🔥", "❤️", "😭"]
            for e in heavy:
                if e in result and random.random() < 0.7:
                    result = result.replace(e, "")
            result = result.strip()

        return result

    def generate_link_reply(self, language: str = "zh") -> str:
        """Return a reply containing the game link (for when someone asks).

        Args:
            language: "zh", "en", "ru", or "vi".

        Returns:
            A message string that always contains the Mini App link.
        """
        lang = language if language in ("zh", "en", "ru", "vi") else "zh"
        pool = _LINK_REPLY.get(lang, _LINK_REPLY["zh"])
        return self._pick_template(pool)

    def check_spam_score(self, content: str) -> float:
        """Rule-based spam score evaluation (0.0 = natural, 1.0 = obvious spam).

        Checks for:
        - URLs and links
        - Marketing buzzwords
        - Excessive emojis
        - Repetitive patterns
        - Overly promotional tone
        """
        score = 0.0
        text_lower = content.lower()

        # 1. Links / URLs
        if re.search(r"https?://|t\.me/|bit\.ly|tinyurl|discord\.gg", text_lower):
            score += 0.35

        # 2. Marketing phrases (zh + en)
        spam_phrases_zh = [
            "百倍", "财富密码", "上车", "稳赚", "包赚", "注册链接",
            "立即注册", "免费领取", "限时", "错过就没了", "日赚",
            "躺赚", "保本", "零风险", "翻倍", "暴富",
        ]
        spam_phrases_en = [
            "100x", "guaranteed", "sign up now", "join now", "free money",
            "act now", "limited time", "don't miss", "get rich", "risk free",
            "no risk", "double your", "passive income", "hurry",
        ]
        all_spam = spam_phrases_zh + spam_phrases_en
        hits = sum(1 for phrase in all_spam if phrase in text_lower)
        score += min(hits * 0.15, 0.5)

        # 3. Excessive emojis (>6 in short message)
        emoji_count = len(re.findall(r"[\U0001f300-\U0001f9ff\U00002600-\U000027bf]", content))
        if emoji_count > 6 and len(content) < 120:
            score += 0.1

        # 4. Repetitive characters (e.g., "!!!!!!!" or "赚赚赚赚赚")
        if re.search(r"(.)\1{4,}", content):
            score += 0.1

        # 5. Call-to-action patterns
        cta_patterns = [
            r"点击.*链接", r"click.*link", r"加入.*群", r"join.*group",
            r"转发.*朋友", r"share.*friend", r"注册.*送",
        ]
        for pat in cta_patterns:
            if re.search(pat, text_lower):
                score += 0.15
                break

        # 6. Too many exclamation marks
        excl_count = content.count("!") + content.count("！")
        if excl_count > 5:
            score += 0.05

        # 7. Very short messages are less likely spam
        if len(content.strip()) < 8:
            score = max(score - 0.15, 0.0)

        # 8. Contains phone number patterns
        if re.search(r"\b\d{10,}\b", content):
            score += 0.2

        return round(max(0.0, min(1.0, score)), 3)
