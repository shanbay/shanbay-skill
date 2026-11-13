<sub>🌐 <a href="README.md">中文</a> · <b>English</b></sub>

<div align="center">

# Shanbay Skill

> *"Hit an unknown word. Say one sentence. It's in your Shanbay."*

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![skills.sh](https://img.shields.io/badge/skills.sh-Compatible-green)](https://skills.sh)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](scripts/shanbay.py)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](scripts/shanbay.py)

<br>

**Check your progress, save new words, and turn a paper's vocabulary into a word book, all from your AI assistant.**

<br>

Stuck on a word while reading a paper? Say "add it to my vocabulary list" and it lands in your Shanbay collection.<br>
Finished the paper? Say "make a word book from the new words", and it's ready to study in Shanbay tomorrow.<br>
No app switching, no copy-paste.

```bash
npx skills add shanbay/shanbay-skill
```

Works with any AI assistant that can run shell commands: Claude Code, Codex, Cursor, OpenCode, and more.

[Install](#install) · [What it does](#what-it-does)

</div>

> [Shanbay](https://www.shanbay.com) (扇贝单词) is a Chinese vocabulary-learning app. This skill talks to your own Shanbay account through the Shanbay OpenAPI. The skill's instructions and messages are written in Chinese.

---

## Install

**Step 1: get an API Key.** Open the [Shanbay Skill page](https://web.shanbay.com/web/main/skill), sign in, and click 「获取 API Key」 (Get API Key).

**Step 2: install the skill.** Pick one.

Paste this to your AI assistant and let it install itself (easiest):

```
帮我安装扇贝 Skill
Skill 地址：https://github.com/shanbay/shanbay-skill
API Key 获取地址：https://web.shanbay.com/web/main/skill
```

Or use the [skills](https://skills.sh) CLI:

```bash
npx skills add shanbay/shanbay-skill
```

Or clone it into your skills directory (Claude Code shown):

```bash
git clone https://github.com/shanbay/shanbay-skill.git ~/.claude/skills/shanbay
```

**Step 3: give the API Key to your assistant.** It asks the first time. Then try:

```
我今天背完了吗？ (Have I finished today's words?)
```

> Requires Python 3.8+. Standard library only, nothing to install.

---

## What it does

| You say | The assistant |
|---|---|
| "Have I finished today?" "How many are left?" | Checks today's progress on the book you're studying |
| "Which words did I struggle with today?" | Lists today's unfamiliar words |
| "Quiz me on today's words" | Makes quizzes or short stories from today's words |
| "Add serendipity and ephemeral to my vocabulary list" | Adds them to your collection, skipping ones already there |
| "Make a word book from the new words in this paper" | Picks the new words and creates a custom word book, or appends to an existing one |
| "Write a memory hook for abandon" | Saves it as a Shanbay word note, appended to your existing note |
| "Switch my current book to the postgraduate exam list" | Changes the book you're studying |

---

## License

[MIT](LICENSE). Use, modify and distribute freely, including commercially.

---

<div align="center">

[Shanbay](https://www.shanbay.com) · [Shanbay Skill page](https://web.shanbay.com/web/main/skill)

</div>
