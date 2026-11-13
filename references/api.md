# 扇贝 OpenAPI 接口参考

`scripts/shanbay.py` 已经封装了下面所有接口。只有脚本没覆盖的读取需求才需要看这里，用 `shanbay api <路径> --query k=v` 调。

- 地址：`https://openapi.shanbay.com`
- 鉴权：`Authorization: Bearer <API Key>`，Key 形如 `osb_pat_` + 43 位
- 只放行下表列出的「方法 + 路径」，其余一律 404（空 body）。同一路径上的 DELETE 也不放行
- 只能读写 Key 主人自己的数据
- 路径里的 id 都是小写字母 hashid
- 频率：每用户每 10 秒 20 次、每 5 小时 2000 次。超了网关直接回 429：空 body、带 `x-envoy-ratelimited: true`，请求没到后端，什么都没执行。业务上的次数上限（如 W2 每天 6000 词）也回 429，但带 `msg`

## 读取

| # | 方法 路径 | 参数 | 返回要点 |
|-|-|-|-|
| — | `GET /bayuser/user` | — | `id`、`nickname`、`username` |
| R1 | `GET /wordscollection/user_words_collection_books` | — | `objects[]`：`book_type`（后续用它指定生词本，0 是默认生词本）、`book_name`、`total_count`、`learned_count` |
| R2 | `GET /wordscollection/words` | `book_type`、`page`、`ipp`、`lite=true` | `objects[].vocabulary.word`、`.id`；`objects[].objects.created_at` 是加入时间；按加入时间从新到旧。不开 `lite` 时 `ipp` 最多 10，开了最多 1500。不传 `business_id` 返回全部来源的词；传 `business_id=6` 只返回从扇贝单词加的 |
| R3 | `GET /wordslearning/custom_books` | `page`、`ipp` | `objects[]`：`id`、`name`、`total_count`、`tag[]`。包含已删除的书，没有状态字段 |
| R4 | `GET /bay-openapi/custom_books/{book_id}/words` | `page`、`ipp`（≤100） | `objects[]`：`id`（单词 id）、`word`、`learned`；`total`。只认书桌上的自定义书，其余 404「单词书不存在」。偶发 500，重试即可 |
| R5 | `GET /wordsapp/user_vocab_notes` | `ipp`（必填，≤20）、`page`（必填）、`note_type=1`（只看自己写的）、`vocab_id` | `objects[]`：外层 `id` 是笔记 id（PUT 用它），`vocab_note.content`、`vocab_note.vocab_id`。按单词查要同时带 `vocab_id` 和 `note_type=1` |
| R6 | `GET /wordsapp/user_material_books/{book_id}/learning/statuses` | — | `a_count`/`a_finished_count` 新词总数/已学，`c_count`/`c_finished_count` 复习词，`is_finished`、`learning_time`（秒）、`date`。`book_id` 是 R9 的 `materialbook_id`。当天第一次调用常返回 412，几秒后再调；第一次调用会为这本书生成今天的任务 |
| R7 | `GET /bay-openapi/user_material_books/{book_id}/today_words` | `type_of`（`NEW`/`REVIEW`）、`only_unknown`、`page`、`ipp`（≤50） | `objects[]`：`id`、`word`、`type`、`today_unknown_count`、`unknown_count`（累计 ≥3 才有值）；`total`。412 时先调 R6 |
| R8 | `GET /wordsapp/tags` | — | `objects[]`：`id`、`name`。建书时从这里选分类 |
| R9 | `GET /wordsapp/user_material_books` | — | 一次返回书桌上全部的书。`objects[]`：`materialbook_id`（R4/R6/R7 用它，不是外层 `id`）、`materialbook.name`、`materialbook.is_custom`、`new_count`/`review_count`（每天的量）、`finished_count`、`total_count`、`updated_at`。不标哪本是当前在学的 |
| R9′ | `GET /wordsapp/user_material_books/current` | — | 用户在扇贝里设的当前在学的书，字段同 R9 的一条（`materialbook_id`、`materialbook.name`……）。没设返回 404「不存在当前学习单词书，请设置」。同一路径的 PUT 切换当前书（W6） |
| — | `GET /abc/words/senses` | `vocabulary_content=<拼写>` | `id` 是单词 id（写笔记用）。没收录返回 404「单词没找到」 |

## 写入（只通过脚本的专用命令调用）

| # | 步骤 | 要点 |
|-|-|-|
| W1 | `POST /wordscollection/user_words_collection_books` `{book_name}` | 非会员只能自建 1 本 |
| W2 | ① `POST /wordscollection/words_bulk_upload?book_type=` `{business_id: 6, words: [...]}` → `task_id` ② `GET /wordscollection/words_bulk_upload?task_id=&business_id=6&book_type=` 轮询 | `status`：1 传入、2 检查、3 上传、4 完成、5 失败；完成时 `failed_words` 是没收录的词。每天最多 6000 词，超了 429 |
| W3 | ① `POST /wordslearning/custom_books/vocabs` `{vocabs: [...]}` → `task_id` ② `GET /wordslearning/custom_books/vocabs?task_id=` 轮询，处理中返回 412，完成返回 `invalid_vocabs`、`repeat_vocabs` ③ `POST /wordslearning/custom_books` `{task_id, name, tag_ids: [tag_id]}` | 书名 ≤20 字、过审核（审核期间 `name` 带「(审核中)」）；每本最多 6000 词；返回新书 `id`，它同时是 R4/R6/R7 用的 `book_id`。**新书不会自动加到书桌**，加到书桌前 R4 读不出词 |
| W4 | ①② 同 W3 ③ `PUT /wordslearning/custom_books/{book_id}` `{task_id}` ④ 返回 `update_task_id` 时轮询 `GET …/{book_id}/update_status?update_task_id=`（`status` 0 完成、1 进行中、2 等待、3 不存在） | 要单词会员；PUT 只传 `task_id`。当天第一次编辑改原书（实测同步完成，返回同一个 `id`）；**当天再编辑会另建一本新书**（原书的词加这次的词），书不在书桌上时还会同时返回 404 `BookUpdateCustomBookOld … NotFound`，但新书已经建出来了。判断要靠前后对比 R3 列表 |
| W5 | 查单词 id → R5 看有没有原笔记 → 没有就 `POST /wordsapp/user_vocab_notes` `{vocab_id, content, content_type: 1}`，有就 `PUT /wordsapp/user_vocab_notes/{笔记 id}` `{content}` | 内容 1–1024 字，`content_type` 1 是 Markdown；要过内容审核 |
| W6 | `PUT /wordsapp/user_material_books/current` `{materialbook_id}` | 换当前在学的书。`materialbook_id` 取 R9 的 `materialbook_id`，只能是书桌上的书（不在书桌上会报错，不会顺带加书桌；不存在的书实测 404「entschema: book not found」，当前书不变）；书桌超出容量时 403「你的书桌已经满啦」 |

## 网页端链接

- 某本书（设每天的量、开始背）：`https://web.shanbay.com/wordsweb/#/books/setting/<book_id>`
- 我的单词书：`https://web.shanbay.com/wordsweb/#/books`
- 生词本：`https://web.shanbay.com/wordsweb/#/collection`
- API Key 管理：`https://web.shanbay.com/web/main/skill`
