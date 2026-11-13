<sub>🌐 <b>中文</b> · <a href="README.en.md">English</a></sub>

<div align="center">

# 扇贝 Skill

> *「读到生词，说一句就进了扇贝。」*

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![skills.sh](https://img.shields.io/badge/skills.sh-Compatible-green)](https://skills.sh)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](scripts/shanbay.py)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](scripts/shanbay.py)

<br>

**在 AI 助手里说一句话，查进度、加生词、把论文生词做成单词书。**

<br>

读英文论文时卡在一个词，说「加到生词本」，它就进了你的扇贝生词本。<br>
一篇论文读完，说「把里面的生词做成单词书」，明天打开扇贝就能背。<br>
不用切 App，不用复制粘贴，也不用记哪个词在哪本书里。

```bash
npx skills add shanbay/shanbay-skill
```

能跑命令行的 AI 助手都能装：Claude Code、Codex、Cursor、OpenCode……

[安装](#装上就能用) · [能做什么](#能做什么)

</div>

---

## 装上就能用

**第 1 步：领 API Key。** 打开 [扇贝 Skill 页面](https://web.shanbay.com/web/main/skill)，登录后点「获取 API Key」。

**第 2 步：装 Skill。** 三种方式选一种。

把这段话发给你的 AI 助手，它会自己装好（最省事）：

```
帮我安装扇贝 Skill
Skill 地址：https://github.com/shanbay/shanbay-skill
API Key 获取地址：https://web.shanbay.com/web/main/skill
```

或者用 [skills](https://skills.sh) 命令行：

```bash
npx skills add shanbay/shanbay-skill
```

或者直接克隆到 skills 目录（以 Claude Code 为例）：

```bash
git clone https://github.com/shanbay/shanbay-skill.git ~/.claude/skills/shanbay
```

**第 3 步：把 API Key 交给 AI。** 第一次用时它会问你要。然后试一句：

```
我今天背完了吗？
```

> 需要 Python 3.8 以上，只用标准库，不用装任何依赖。

---

## 能做什么

| 你说 | AI 会 |
|---|---|
| 「我今天背完了吗？」「还剩多少？」 | 查当前在学的书今天的进度 |
| 「今天哪些词不熟？」 | 列出今天不熟的词 |
| 「拿今天的词给我出 5 道题」 | 用今天的词出题、编故事帮你复习 |
| 「把 serendipity、ephemeral 加到生词本」 | 加进生词本，已经在的自动跳过 |
| 「把这篇论文里的生词做成单词书」 | 挑出生词，建一本自定义单词书；也能往已有的书里追加 |
| 「给 abandon 写个助记」 | 写进扇贝的单词笔记，默认接在原笔记后面 |
| 「把在学的书换成考研词汇」 | 换当前在学的书 |

---

## License

[MIT](LICENSE)。可以自由使用、修改、分发，包括商业用途。

---

<div align="center">

[扇贝单词](https://www.shanbay.com) · [扇贝 Skill 页面](https://web.shanbay.com/web/main/skill)

</div>
