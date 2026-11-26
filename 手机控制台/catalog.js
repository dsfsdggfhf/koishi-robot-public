"use strict";

// Every live boolean switch in config.json appears exactly once below.
// Descriptions explain the existing option; they do not trigger any action.
window.KOISHI_CATALOG = [
  {page:"private",title:"聊天总开关与补查",summary:"决定互动聊天是否参与运行，并控制漏消息补查。",detail:"补查用于降低短暂断线后遗漏消息的概率；总开关关闭时子项也无法独立工作。",paths:["interactive_chat","interactive_chat.catchup"]},
  {page:"private",title:"私信回复",summary:"让机器人在符合原有规则时处理收到的私信。",detail:"关闭后不再由此模块生成私信回复；配对码登录不会发送私信。",paths:["interactive_chat.private_messages"]},
  {page:"private",title:"管理员指令",summary:"启用已有的管理员命令入口。",detail:"只影响机器人原有的管理员指令逻辑；控制台登录与此开关无关。",paths:["admin_system"]},

  {page:"comments",title:"评论回复",summary:"控制视频评论区的自动回复。",detail:"公开回复会受原有频率与安全规则影响；修改开关不会立即发布评论。",paths:["interactive_chat.comment_replies"]},
  {page:"comments",title:"@ 回复",summary:"处理别人通过 @ 提到机器人的内容。",detail:"此入口与普通评论回复分开，方便单独暂停。",paths:["interactive_chat.at_replies"]},
  {page:"comments",title:"动态评论",summary:"处理动态下的评论互动。",detail:"与视频评论入口分开，便于根据平台状态调整。",paths:["interactive_chat.dynamic_comments"]},
  {page:"comments",title:"评论图片",summary:"允许评论互动使用图片相关处理。",detail:"如果图片识别异常，可先单独关闭这一项排查。",paths:["interactive_chat.comment_images"]},

  {page:"greetings",title:"早安与晚安",summary:"控制日常早安、晚安私信模块。",detail:"两项分别可切换；具体时间、对象和去重仍由机器人原有配置处理。",paths:["morning_greeting","nightly_greeting"]},
  {page:"greetings",title:"新粉问候",summary:"控制新关注用户的问候功能。",detail:"此处只开关模块，实际发送仍依据原有对象与频率规则。",paths:["new_fan_greeting"]},

  {page:"birthdays",title:"生日提醒",summary:"检查和提醒即将到来的生日。",detail:"用于生日相关流程的提醒入口，不会在切换开关时立即发送祝福。",paths:["birthday_reminder"]},
  {page:"birthdays",title:"生日故事",summary:"为生日祝福启用故事型文案。",detail:"适合需要较长、个性化祝福的场景；文案生成仍按原有调用规则执行。",paths:["birthday_story_greeting"]},
  {page:"birthdays",title:"生日贺卡",summary:"启用生日贺卡功能。",detail:"涉及图片或文案生成时，仍按机器人原有流程和费用设置运行。",paths:["birthday_card"]},

  {page:"content",title:"每周动态",summary:"控制每周报告动态的生成与发布入口。",detail:"关闭后可暂时停用这类周期内容，不影响其他评论回复。",paths:["weekly_report_dynamic"]},
  {page:"content",title:"贴纸",summary:"在支持的内容里启用贴纸素材。",detail:"贴纸素材是否可用还取决于本地资源和原有发送规则。",paths:["stickers"]},

  {page:"station",title:"小站早晚安",summary:"控制小站内的早晚安内容。",detail:"与私信早晚安分开，便于单独管理小站发布节奏。",paths:["station_greeting_posts"]},
  {page:"station",title:"小站评论监控",summary:"关注小站评论区的新互动。",detail:"如果平台限频，可单独暂停监控并查看日志。",paths:["station_comment_monitor"]},
  {page:"station",title:"小站模板池",summary:"使用小站内容的现有模板池。",detail:"模板内容来自项目本地文件；这个开关本身不新增模型调用。",paths:["station_template_pool"]},
  {page:"station",title:"小站配图",summary:"允许小站内容使用配图。",detail:"配图异常时可单独关闭，不必关闭整个小站模块。",paths:["station_images"]},

  {page:"persona",title:"恋恋人格",summary:"启用角色人格相关的表达规则。",detail:"此项影响生成内容的口吻；具体提示词仍由原机器人管理。",paths:["persona"]},
  {page:"persona",title:"歌词点缀",summary:"允许在适当情境下引用项目中的歌曲元素。",detail:"歌词功能和歌曲识别是不同入口；关闭这里不会改变识别组件。",paths:["koishi_lyrics"]},
  {page:"persona",title:"表情包",summary:"控制表情素材在互动中的使用。",detail:"发送范围与素材仍遵循机器人原有逻辑。",paths:["emotes"]},
  {page:"persona",title:"形态变化",summary:"启用恋恋不同状态或形态的表达。",detail:"只影响原有形态模块，不改变手机控制台的立绘换装。",paths:["morph"]},

  {page:"knowledge",title:"东方知识库",summary:"为东方设定相关回答提供项目知识。",detail:"开关控制知识入口；知识库文件仍保存在机器人项目中。",paths:["touhou_knowledge"]},
  {page:"knowledge",title:"Wiki 学习",summary:"控制从 Wiki 资料学习的既有流程。",detail:"它可能涉及网络和处理时间，遇到来源异常时可独立暂停。",paths:["wiki_learning"]},
  {page:"knowledge",title:"知识辅助",summary:"允许回答时使用知识辅助模块。",detail:"用于提高具体设定问题的依据性，仍按原有模型路由执行。",paths:["knowledge_assist"]},

  {page:"learning",title:"长期学习",summary:"启用机器人长期积累经验的模块。",detail:"它与弹幕游戏训练无关；这里只有社交机器人的学习开关。",paths:["touhou_learning"]},
  {page:"learning",title:"对话记忆",summary:"启用互动中的记忆增强。",detail:"关闭后可用于排查旧记忆对回复的干扰。",paths:["memory_enhance"]},
  {page:"learning",title:"学习日报",summary:"控制每日学习报告。",detail:"日报是汇总展示入口，不等于所有学习功能的总开关。",paths:["learn_daily_report"]},
  {page:"learning",title:"学习任务",summary:"启用已有的学习任务调度。",detail:"与长期学习模块分开，便于观察任务本身是否稳定。",paths:["learn_task"]},

  {page:"media",title:"视频理解与下载",summary:"控制视频理解，以及唧唧下载和备用下载两条本地获取路径。",detail:"关闭视频理解总开关时下载子项不会独立完成理解；下载工具能减少重复获取，但不直接减少模型分析费用。",paths:["media_understanding","media_understanding.jijidown","media_understanding.bilidown"]},

  {page:"recognition",title:"角色识别",summary:"从视频或图片中尝试识别角色。",detail:"小图、遮挡和背景相似时误认仍可能发生，可结合样本匹配结果检查。",paths:["media_understanding.character_recognition"]},
  {page:"recognition",title:"本地样本匹配",summary:"使用已有样本辅助角色识别。",detail:"依赖本地样本质量；关闭可对照模型识别的实际表现。",paths:["media_understanding.character_sample_matching"]},
  {page:"recognition",title:"媒体纠错样本",summary:"在媒体理解中使用纠错样本图片。",detail:"与主纠错学习模块分开，可单独排查样本图带来的误识别。",paths:["media_understanding.correction_learning.sample_images"]},
  {page:"recognition",title:"纠错学习与样本",summary:"控制纠错学习总入口和样本图片使用。",detail:"两项分别可切换；应先核对样本标注质量，再判断识别是否改善。",paths:["correction_learning","correction_learning.sample_images"]},
  {page:"recognition",title:"私信识图",summary:"允许私信入口处理角色图片。",detail:"关闭后仍可保留普通私信回复，适合单独排查图片识别错误。",paths:["interactive_chat.role_recognition_dm"]},

  {page:"safety",title:"东方内容检查",summary:"启用东方设定和内容相关检查。",detail:"公开内容生成前的检查应保持与现有发布流程一致。",paths:["touhou_compliance"]},
  {page:"safety",title:"评论标签检查",summary:"检查评论中的标签或风险标记。",detail:"用于降低不合适内容进入公开评论的概率。",paths:["comment_tag_safety"]},
  {page:"safety",title:"模型调用与文字试用",summary:"控制 AI 总入口与 GPT-6.1 Sol 文字试用。",detail:"关闭 AI 总入口会影响依赖模型的功能；单独关闭文字试用则恢复原 5.6 Sol 文字链。试用只影响原 5.6 Sol 的纯文字任务，看图和指定模型步骤仍按原配置执行。手机控制台自身不调用模型。",paths:["ai","ai.premium_text_only_trial"]},

  {page:"health",title:"健康检查",summary:"启用已有的运行健康检查。",detail:"与控制台状态读取独立；状态页仍可查看本地进程是否存在。",paths:["health_check"]},
  {page:"health",title:"心跳报告",summary:"控制机器人的周期心跳报告。",detail:"如果报告过于频繁，可单独关闭此模块观察。",paths:["heartbeat"]},
];
