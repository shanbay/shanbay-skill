# 安全与数据流

这份说明列出扇贝 Skill 会访问什么、在本机留下什么。所有结论都能对着 [`scripts/shanbay.py`](scripts/shanbay.py) 核对。

## 出站请求

只访问一个域名：`openapi.shanbay.com`（可用环境变量 `SHANBAY_API_BASE` 改掉）。没有统计上报，不访问任何第三方。

脚本里出现的 `web.shanbay.com` 链接只是拼给你点的网页地址，脚本自己不请求它们。

## 会调用的接口

读取：

| 接口 | 用途 |
|---|---|
| `GET /bayuser/user` | 确认 API Key 有效、对应哪个账号 |
| `GET /wordsapp/user_material_books`、`/current` | 书桌上的书、当前在学的书 |
| `GET /wordsapp/user_material_books/{id}/learning/statuses` | 今天的进度 |
| `GET /bay-openapi/user_material_books/{id}/today_words` | 今天要背的词 |
| `GET /wordslearning/custom_books`、`/bay-openapi/custom_books/{id}/words` | 自建的单词书和书里的词 |
| `GET /wordscollection/user_words_collection_books`、`/wordscollection/words` | 生词本和里面的词 |
| `GET /wordsapp/user_vocab_notes` | 自己写的单词笔记 |
| `GET /wordsapp/tags`、`/abc/words/senses` | 单词书分类、按拼写查单词 id |

写入（都要用户同意，见下一节）：

| 接口 | 用途 |
|---|---|
| `POST /wordscollection/user_words_collection_books` | 新建生词本 |
| `POST /wordscollection/words_bulk_upload` | 往生词本加词 |
| `POST /wordslearning/custom_books`、`PUT /wordslearning/custom_books/{id}` | 建单词书、往单词书追加单词 |
| `POST /wordsapp/user_vocab_notes`、`PUT /wordsapp/user_vocab_notes/{id}` | 写单词笔记 |
| `PUT /wordsapp/user_material_books/current` | 换当前在学的书 |

`POST /wordslearning/custom_books/vocabs` 只让扇贝解析词表、看哪些词认得，不改你的数据。

没有任何删除操作。`api` 子命令可以直接调开放接口，但只允许 GET。

## 写入要用户同意

写入命令不带 `--yes` 时只输出预览。带 `--yes` 时，后面必须跟用户同意时的原话；原话里看不出同意，或者听起来是拒绝，脚本就不写。这一步在脚本里检查，不靠模型自觉。

## API Key

- API Key 代表你本人，只能读写你自己的数据，不能登录扇贝。
- `auth set -` 从标准输入读 Key，先向扇贝确认有效，再存到 `~/.config/shanbay/api_key`，文件权限 600。Key 不会出现在命令行参数和进程列表里。
- 也可以用环境变量 `SHANBAY_API_KEY` 或 `SHANBAY_KEY_FILE` 指定，优先于默认文件。
- 脚本输出里的 Key 都会打码，只显示开头和末尾几位。
- 不想用了：删掉 `~/.config/shanbay/api_key`，再到 [扇贝 Skill 页面](https://web.shanbay.com/web/main/skill) 重建 Key，旧 Key 立刻失效。

## 本机留下的文件

| 位置 | 内容 | 保留多久 |
|---|---|---|
| `~/.config/shanbay/api_key` | API Key | 直到你删掉 |
| 系统临时目录 `shanbay-skill-*` | 书桌列表（3 分钟）、单词书分类（1 小时）、词表解析结果、限流记录、写笔记时的文件锁 | 临时，删掉不影响使用 |
| `SHANBAY_LOG` 指定的文件 | 每次请求的方法、路径、状态码，不含 Key | 只有设了这个环境变量才写 |

## 报告安全问题

请不要在公开 issue 里写漏洞细节。先开一个不含细节的 issue 说明要报告安全问题，我们会联系你。
