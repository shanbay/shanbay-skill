#!/usr/bin/env python3
"""扇贝 OpenAPI 命令行，给 AI 助手用：读写用户自己的扇贝单词数据。

只依赖 Python 3.8+ 标准库。每条命令往 stdout 打一段 JSON；出错时往 stderr
打一句可以直接转述给用户的中文，并以非 0 退出。

写入类命令（加生词本、建生词本、建单词书、追加单词、写笔记）不带 --yes 时
只打印预览、不写入：先把预览给用户看，用户同意后再带 --yes "<用户同意时的原话>"
运行同一条命令。--yes 后面的原话里看不出同意，脚本不写。

为了让各家模型都能用对，能在脚本里定死的规则都放在脚本里：加生词自动跳过已经在
生词本里的词；progress / today 可以直接按书名找书，对上多本就停下让 AI 去问；
建书 / 追加直接收词表，分批解析、识别不了超过三成先停，都由脚本处理。
"""

import argparse
import http.client
import json
import math
import os
import pathlib
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse

VERSION = "0.1.0"
API_BASE = os.environ.get("SHANBAY_API_BASE", "https://openapi.shanbay.com")
# 调试用：openapi.shanbay.com 的 DNS 没配好时，把域名钉到网关 IP（相当于 curl --resolve）
PIN_IP = os.environ.get("SHANBAY_API_IP", "").strip()
KEY_FILE = pathlib.Path(
    os.environ.get("SHANBAY_KEY_FILE", "~/.config/shanbay/api_key")
).expanduser()
KEY_PAGE = "https://web.shanbay.com/web/main/skill"
WEB_BOOK_URL = "https://web.shanbay.com/wordsweb/#/books/setting/{book_id}"
WEB_BOOKS_URL = "https://web.shanbay.com/wordsweb/#/books"
WEB_COLLECTION_URL = "https://web.shanbay.com/wordsweb/#/collection"
# 评测 / 调试用：把每次请求记到 JSONL；拦截写入（带 --yes 也不真的写）
LOG_FILE = os.environ.get("SHANBAY_LOG", "").strip()
BLOCK_WRITES = os.environ.get("SHANBAY_BLOCK_WRITES", "").strip() not in ("", "0")
KEY_RE = re.compile(r"^osb_pat_[A-Za-z0-9_-]{43}$")
MAX_WORDS = 1000          # Skill 守则：单次提交不超过 1000 个词
NOTE_MAX = 1024           # 单词笔记上限
BOOK_NAME_MAX = 20        # words-learning 建书接口的 name maxLength
WORDS_BUSINESS_ID = 6     # 生词本接口里「扇贝单词」的业务编号


# ---------------------------------------------------------------- 输出与错误

class ApiError(Exception):
    def __init__(self, status, msg, body=None, path="", method="GET", rate_limited=False, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.msg = msg or ""
        self.body = body
        self.path = path
        self.method = method
        # 网关限流（Envoy 直接回的 429：带 x-envoy-ratelimited 头、没有 body），请求没到扇贝的服务。
        # 业务上限（比如生词本每天加词数）也是 429，但带 msg，不算在这里。
        self.rate_limited = rate_limited
        self.retry_after = retry_after   # 秒；扇贝给了恢复时间才有
        self.waited = 0                  # 脚本为它等了多少秒
        self.recent = False              # 几分钟内已经确认过次数用完，没再等


def read_text_any(path):
    """用户的词表、笔记文件可能是 UTF-8、带 BOM 的 UTF-8，或者 Windows 上常见的 GBK。"""
    data = pathlib.Path(path).expanduser().read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def log(entry):
    if not LOG_FILE:
        return
    entry = {"ts": time.strftime("%H:%M:%S"), **entry}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def blocked(action, result=None, **payload):
    """评测环境里拦下真正的写入，返回 True 时调用方直接 return。result 是假装成功时的返回。"""
    if not BLOCK_WRITES:
        return False
    log({"blocked_write": action, **payload})
    out({"ok": True, "blocked": True, "action": action, **(result or {}),
         "note": "评测环境拦截了这次写入（SHANBAY_BLOCK_WRITES），按写入成功继续。"})
    return True


def fail(text, code=2):
    print(text, file=sys.stderr)
    sys.exit(code)


def explain(e):
    s, m = e.status, e.msg
    writing = e.method != "GET"
    if s == 0:
        if writing:
            return (f"写入时和扇贝 OpenAPI 的连接断了（{m}），可能写进去了也可能没有。"
                    f"先用对应的列表命令查一下，确认没写进去再重试。")
        return (f"连不上扇贝 OpenAPI（{m}），脚本已经自动重试过。这次不要再试：告诉用户扇贝 OpenAPI 暂时连不上、"
                f"和他的 Key 无关，过一会儿再问；急的话可以先在扇贝 App 或网页端看。")
    if s == 401:
        # 没过期的 Key 不会 401；到这一步续期已经没用了，只能重建
        return (f"扇贝 API Key 失效了（已过期、被删除，或者之后又点过「重建」）。这时候只能重建：请用户到 {KEY_PAGE} "
                f"点「重建」，把新的 Key 发过来。这个任务里先别再调扇贝。")
    if s == 404 and not e.body:
        if not gateway_ok(e.path):
            return ("扇贝 OpenAPI 现在连不上（没有正常响应），不是 Key 或请求的问题。这次不要再试：告诉用户扇贝 OpenAPI "
                    "暂时不可用、和他的 Key 无关，过一会儿再问；一直这样请联系扇贝。")
        return "这个接口没有对 OpenAPI 开放（网关拒绝）。不要换路径或方法重试。"
    if s == 412:
        return "今天的学习数据还没准备好。等几秒再查一次。"
    if s == 429:
        if e.rate_limited:
            if e.recent:
                head = "扇贝 OpenAPI 还在限流，几分钟前已经确认过这段时间的调用次数用完了。"
            elif e.waited:
                head = (f"扇贝 OpenAPI 限流：脚本等了 {e.waited:.0f} 秒再试还是被拒，"
                        f"多半是这段时间的调用次数用完了（同一账号的所有 Key、所有工具共用每 5 小时 2000 次，换 Key 也一样）。")
            else:
                head = "扇贝 OpenAPI 限流，这段时间的调用次数用完了（同一账号的所有 Key、所有工具共用每 5 小时 2000 次，换 Key 也一样）。"
            when = (f"大约 {math.ceil(e.retry_after / 60)} 分钟后恢复。" if e.retry_after
                    else "扇贝没说什么时候恢复，最长要等 5 小时。")
            return (head + "这次请求没有执行，数据没有变。" + when
                    + "先停下，这个任务里别再调扇贝，也不要反复重试。")
        return (f"扇贝拒绝了这次请求：{m or '超出次数上限'}。请求没有执行，数据没有变；"
                f"马上重试结果也一样，照扇贝的原话转告用户。")
    if s >= 500:
        if writing:
            return (f"写入时扇贝服务出错（{s} {m}），可能写进去了也可能没有。"
                    f"先用对应的列表命令查一下，确认没写进去再重试。")
        return f"扇贝服务暂时出错（{s} {m}）。稍后再试。"
    return f"扇贝返回 {s}：{m}"


def gateway_ok(failed_path):
    """空 body 的 404 有两种来源：接口没开放，或者域名根本没指到 OpenAPI 网关（比如 DNS 没配好）。
    /bayuser/user 永远开放，探一下就能分清。"""
    if failed_path.split("?")[0] == "/bayuser/user":
        return False
    try:
        request("GET", "/bayuser/user")
        return True
    except ApiError as e:
        return not (e.status == 404 and not e.body)
    except SystemExit:
        return True


# ---------------------------------------------------------------- HTTP

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到 PIN_IP，但 SNI 和证书校验仍按原域名。"""

    def connect(self):
        sock = socket.create_connection((PIN_IP, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def load_key(required=True):
    env = os.environ.get("SHANBAY_API_KEY", "").strip()
    if env:
        return env, "env:SHANBAY_API_KEY"
    if KEY_FILE.exists():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k, str(KEY_FILE)
    if required:
        fail(f"还没配置扇贝 API Key。请到 {KEY_PAGE} 登录，点「获取 API Key」，把 Key 发过来。", code=3)
    return "", ""


RATE_FILE = pathlib.Path(tempfile.gettempdir()) / "shanbay-skill-rate.json"
RATE_WINDOW, RATE_MAX = 10.0, 16   # 扇贝限每用户 10 秒 20 次，留点余量
# 被网关限流时的等待：扇贝没给恢复时间就先等 15 秒、再等 45 秒（盖住 10 秒和 1 分钟两种窗口），
# 还不行就认定是长时间的额度用完了，记下来，几分钟内别的命令再遇到限流就不再干等。
LIMIT_FILE = pathlib.Path(tempfile.gettempdir()) / "shanbay-skill-429.json"
LIMIT_WAITS = (15, 45)
LIMIT_MAX_WAIT = 60
LIMIT_MEMO = 600


def _limit_until():
    try:
        return float(json.loads(LIMIT_FILE.read_text()).get("until", 0))
    except (OSError, ValueError, AttributeError):
        return 0.0


def _limit_wait(e, tries):
    """返回这次 429 该等几秒；None 表示别等了，直接报错。"""
    if time.time() < _limit_until():
        e.recent = True
        return None
    if e.retry_after is not None:
        return e.retry_after + 0.5 if (e.retry_after <= LIMIT_MAX_WAIT and tries < 2) else None
    return LIMIT_WAITS[tries] if tries < len(LIMIT_WAITS) else None


def _retry_after(resp):
    for h in ("Retry-After", "X-RateLimit-Reset"):
        v = resp.getheader(h)
        if v:
            try:
                return max(0.0, float(v))
            except ValueError:
                continue
    return None


def throttle():
    """跨进程的简单限速：每条命令是独立进程，用临时文件记最近的请求时间。
    有 fcntl（macOS / Linux）时加文件锁，并发跑也不会一起冲过去；没有就尽力而为。"""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        f = open(RATE_FILE, "a+")
    except OSError:
        return
    with f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            stamps = json.loads(f.read() or "[]")
        except ValueError:
            stamps = []
        now = time.time()
        stamps = [t for t in stamps if isinstance(t, (int, float)) and now - t < RATE_WINDOW]
        if len(stamps) >= RATE_MAX:
            time.sleep(max(0.0, RATE_WINDOW - (now - stamps[0])) + 0.1)
            now = time.time()
            stamps = [t for t in stamps if now - t < RATE_WINDOW]
        stamps.append(now)
        f.seek(0)
        f.truncate()
        f.write(json.dumps(stamps))
        f.flush()
        # 锁随 with 退出时关闭文件一起释放


def request(method, path, query=None, body=None, key=None):
    if key is None:
        key, _ = load_key()
    throttle()
    base = urllib.parse.urlsplit(API_BASE)
    if query:
        q = {k: v for k, v in query.items() if v is not None}
        if q:
            path = path + "?" + urllib.parse.urlencode(q)
    full_path = base.path.rstrip("/") + path
    if base.scheme == "http":  # 只给本地模拟服务测试用
        conn = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=30)
    else:
        cls = _PinnedHTTPSConnection if PIN_IP else http.client.HTTPSConnection
        conn = cls(base.hostname, base.port or 443, timeout=30,
                   context=ssl.create_default_context())
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": f"shanbay-skill/{VERSION}",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, full_path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        log({"method": method, "path": path, "status": 0, "error": str(exc)})
        raise ApiError(0, str(exc), None, path, method)
    finally:
        conn.close()
    log({"method": method, "path": path, "status": resp.status})
    if 200 <= resp.status < 300 and LIMIT_FILE.exists():
        try:
            LIMIT_FILE.unlink()   # 额度恢复了
        except OSError:
            pass
    parsed = None
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
    if 200 <= resp.status < 300:
        return parsed
    msg = ""
    if isinstance(parsed, dict):
        msg = parsed.get("msg") or parsed.get("message") or ""
    elif raw.strip():
        msg = raw.strip()[:200]
    limited = resp.status == 429 and (resp.getheader("x-envoy-ratelimited") is not None or not msg)
    raise ApiError(resp.status, msg, parsed if parsed is not None else (raw.strip() or None), path, method,
                   rate_limited=limited, retry_after=_retry_after(resp) if resp.status == 429 else None)


def call(method, path, query=None, body=None, retry_412=0, retry_5xx=None, wait=2.0):
    """发请求；GET 遇到 5xx / 网络错误重试，按需重试 412。
    被网关限流（429）时读写都等一会儿重试：限流的请求没到扇贝的服务，重试不会重复写入。
    其余情况写请求不自动重试。"""
    if retry_5xx is None:
        retry_5xx = 2 if method == "GET" else 0
    n412 = n5xx = n429 = 0
    waited = 0.0
    while True:
        try:
            return request(method, path, query, body)
        except ApiError as e:
            if e.status == 429 and e.rate_limited:
                pause = _limit_wait(e, n429)
                if pause is None:
                    e.waited = waited
                    if not e.recent:
                        try:
                            LIMIT_FILE.write_text(json.dumps({"until": time.time() + (e.retry_after or LIMIT_MEMO)}))
                        except OSError:
                            pass
                    raise
                n429 += 1
                waited += pause
                print(f"扇贝限流（调用太频繁），等 {pause:.0f} 秒再试……", file=sys.stderr, flush=True)
                time.sleep(pause)
                continue
            if e.status == 412 and n412 < retry_412:
                n412 += 1
                time.sleep(wait)
                continue
            if (e.status == 0 or e.status >= 500) and n5xx < retry_5xx:
                n5xx += 1
                time.sleep(1.5 * n5xx)
                continue
            raise


def run(fn, args):
    try:
        fn(args)
    except ApiError as e:
        fail(explain(e))


# ---------------------------------------------------------------- 工具函数

POS_TAIL = re.compile(r"(\s+(n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|abbr|phr|aux)\.?)+$", re.I)


def _clean_token(tok):
    """从一行里取出英文词或短语：兼容「abandon v. 放弃」「1. abandon」「abandon /əˈbændən/」这类常见词表格式。"""
    tok = tok.strip().strip("•·-*").strip()
    tok = re.sub(r"^\(?\d+[\.)、]?\s*", "", tok)          # 行首编号
    m = re.match(r"[A-Za-z][A-Za-z\-' ]*", tok)
    if not m:
        return ""
    word = POS_TAIL.sub("", m.group(0).strip()).strip(" -'")
    return re.sub(r"\s+", " ", word)


def read_words(args, cap=True):
    """从位置参数和 --file 读单词：按行、逗号、分号拆开，每段取开头的英文词或短语，去空、去重（保留首次出现的大小写）。
    cap=False 时不卡 1000 个（建书时由脚本自己分批解析）。"""
    items = list(getattr(args, "words", None) or [])
    if getattr(args, "file", None):
        p = pathlib.Path(args.file).expanduser()
        if not p.exists():
            fail(f"找不到词表文件：{p}", code=4)
        items.append(read_text_any(p))
    words, seen, skipped = [], set(), []
    for chunk in items:
        for raw in re.split(r"[\n,;，；、\t]+", chunk):
            if not raw.strip():
                continue
            w = _clean_token(raw)
            if not w:
                skipped.append(raw.strip()[:30])
                continue
            key = w.lower()
            if key in seen:
                continue
            seen.add(key)
            words.append(w)
    if skipped:
        print(json.dumps({"skipped_lines": skipped[:20], "skipped_count": len(skipped),
                          "note": "这些行里没有英文单词，已跳过"}, ensure_ascii=False), file=sys.stderr)
    if not words:
        fail("没有拿到单词。用空格、逗号或换行分隔，或者用 --file 指定词表文件（一行一个）。", code=4)
    if cap and len(words) > MAX_WORDS:
        fail(f"一次最多提交 {MAX_WORDS} 个词，这次有 {len(words)} 个。请按 {MAX_WORDS} 个一批分几次提交"
             f"（往单词书追加超过 {MAX_WORDS} 个要分几天加）。", code=4)
    return words


TASK_CACHE = pathlib.Path(tempfile.gettempdir()) / "shanbay-skill-tasks.json"


def remember_task(task_id, words):
    """记下 check-words 的 task_id 对应哪些词，建书 / 追加的预览和结果里能列出来。"""
    try:
        data = json.loads(TASK_CACHE.read_text(encoding="utf-8")) if TASK_CACHE.exists() else {}
    except ValueError:
        data = {}
    data[task_id] = {"ts": int(time.time()), "words": words}
    data = dict(sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[-50:])
    try:
        TASK_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def task_words(task_id):
    try:
        return (json.loads(TASK_CACHE.read_text(encoding="utf-8")).get(task_id) or {}).get("words")
    except (OSError, ValueError):
        return None


def preview(action, **info):
    info = {"preview": True, "written": False, "action": action, **info,
            "next": "这是预览，什么都还没写。现在结束这一轮：在回复里把要写的内容给用户看，问他要不要写，然后停下等他回复。"
                    "用户回复同意后，再运行同一条命令并加上 --yes \"<用户同意时的原话>\"。"
                    "（用户在请求里已经说了「直接…」「不用问」，就不用预览，直接带 --yes \"<那句原话>\" 运行。）"}
    out(info)


# 写入前的同意检查：--yes 后面要带用户表示同意的原话。看不出同意就不写——防止 AI 预览完没等用户就接着写。
CONSENT_ZH = ("直接", "确认", "同意", "好", "行", "可以", "对", "嗯", "是的", "没问题", "吧", "就这样", "不用问",
              "别问", "不用确认", "没错", "照这个", "按这个", "都要", "替换", "覆盖", "重写")
CONSENT_EN = re.compile(r"\b(yes|yep|yeah|ok|okay|sure|go ahead|do it|just do it|confirm(ed)?|proceed|fine|"
                        r"sounds good|lgtm|please do|directly|go for it|without asking|no problem|approved?)\b", re.I)


# 否定的话里也常带「吧」「行」「do it」（「别加了吧」「不行，先别加」「don't do it」），先排除
REFUSE_ZH = re.compile(r"别(?!问)|不要(?!问|再问)|不行|不同意|不确认|不可以|不需要|不好|不加|不写|不建|不存|不改|先不|不用(?!问|再问|确认)|取消|算了|等一下|等等|停一下|暂停|再说吧|再想想")
REFUSE_EN = re.compile(r"\b(don'?t|do not|not yet|no(?! problem)|cancel|stop|wait|hold on|never mind|nevermind)\b", re.I)


def consent(args):
    """没带 --yes 返回 False（走预览）；带了且原话里看得出同意返回 True；看不出同意就报错退出，不写。"""
    said = getattr(args, "yes", None)
    if said is None:
        return False
    s = (said or "").strip()
    # 「直接」「不用问」「同意」「确认」是明确的授权，前面没紧跟「不要 / 别 / 不」就算同意（「原来的不要了，直接改」）
    strong = re.search(r"(?<!不要)(?<!别)(?<!不)直接|不用问|别问|(?<!不)同意|(?<!不)确认", s)
    refused = not strong and (REFUSE_ZH.search(s) or REFUSE_EN.search(s))
    hits = [k for k in CONSENT_ZH if k in s]
    # 只靠一个「吧」撑着、又带着「把 / 帮我 / 给我 / 请」的，多半是 AI 把请求改写成了「同意」（「我把这些加进去吧」）
    if hits == ["吧"] and re.search(r"把|帮我|给我|请", s):
        hits = []
    if s and not refused and (hits or CONSENT_EN.search(s)):
        log({"consent": s[:100]})
        return True
    fail(("--yes 后面没带用户的原话" if not s else
          f"「{s[:40]}」听起来是不要写" if refused else f"「{s[:40]}」里看不出用户同意写入") +
         "，这次什么都没写。用户还没同意的话：结束这一轮，把要写的内容给用户看、问他要不要写，等他回复。"
         "他同意后，再运行同一条命令，--yes 后面带上他同意时的原话（比如 --yes \"好，加吧\"）；"
         "用户在请求里已经说了「直接…」「不用问」，就把那句话带上（比如 --yes \"直接加\"）。", code=6)


_CACHE_DIR = pathlib.Path(tempfile.gettempdir())


def _cached(name, ttl):
    """读几分钟内存下的接口结果（同一个对话里几条命令接着跑，不用重复请求）。过期或没有返回 None。"""
    f = _CACHE_DIR / f"shanbay-skill-cache-{name}.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if time.time() - d["ts"] < ttl:
            return d["data"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _store(name, data):
    try:
        (_CACHE_DIR / f"shanbay-skill-cache-{name}.json").write_text(
            json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def desk_books(recent_ok=False):
    """书桌上的书。recent_ok=True 时 3 分钟内复用刚取过的列表——只用于按书名找书；
    列书、判断书在不在书桌上一律现取（用户可能刚把书加到书桌）。"""
    if recent_ok:
        hit = _cached("desk", 180)
        if hit is not None:
            return hit
    data = call("GET", "/wordsapp/user_material_books") or {}
    objs = data.get("objects") or []
    _store("desk", objs)
    return objs


def all_custom_books():
    books, page = [], 1
    while True:
        data = call("GET", "/wordslearning/custom_books", {"ipp": 50, "page": page}) or {}
        objs = data.get("objects") or []
        books.extend(objs)
        if not objs or len(books) >= data.get("total", 0):
            break
        page += 1
    return books


def collection_books():
    data = call("GET", "/wordscollection/user_words_collection_books") or {}
    return data.get("objects") or []


def find_collection(book):
    books = collection_books()
    for b in books:
        if str(b.get("book_type")) == str(book) or b.get("book_name") == book or b.get("id") == book:
            return b
    names = "、".join(f"{b.get('book_name')}（book_type={b.get('book_type')}）" for b in books)
    fail(f"没找到生词本「{book}」。现有的生词本：{names}", code=4)


def lookup_vocab(word):
    try:
        data = call("GET", "/abc/words/senses", {"vocabulary_content": word})
    except ApiError as e:
        if e.status == 404:
            fail(f"扇贝词库里没找到「{word}」。检查拼写，或换成原形再试。", code=4)
        raise
    return {"word": data.get("content", word), "vocab_id": data.get("id")}


def own_notes(vocab_id):
    data = call("GET", "/wordsapp/user_vocab_notes",
                {"vocab_id": vocab_id, "note_type": 1, "ipp": 20, "page": 1}) or {}
    return data.get("objects") or []


# ---------------------------------------------------------------- 命令：配置

def cmd_auth_set(args):
    key = args.key.strip()
    if key == "-":
        key = sys.stdin.read().strip()
    if not KEY_RE.match(key):
        fail("这不像扇贝 API Key：应该是 osb_pat_ 开头、后面 43 位字母数字，这串的长度或字符不对，没有保存；"
             "原来配置的 Key 不受影响。请用户从 Skill 页面把 Key 整串重新复制过来。回复里不要复述这串 Key。", code=4)
    limited = None
    try:
        user = request("GET", "/bayuser/user", key=key) or {}
    except ApiError as e:
        user = {}
        if e.status == 429:   # 网关先验 Key 再限流，被限流说明 Key 是有效的
            limited = e
        elif e.status == 401:
            had = "原来配置的 Key 不受影响。" if load_key(required=False)[0] else ""
            fail(f"这串 Key 验证没通过，没有保存。{had}常见原因是没复制全，或者之后又点过「重建」让这串作废了；"
                 f"也可能已经过期。请到 {KEY_PAGE} 把页面上现在的 Key 整串复制过来，过期了就先点「重建」。")
        else:
            fail(explain(e))
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    out({"ok": True, "nickname": user.get("nickname"), "saved_to": str(KEY_FILE),
         **({"rate_limited": "Key 有效、已经保存，但这个账号现在被扇贝限流（调用太频繁），过一会儿才能用。"}
            if limited else {}),
         "note": "API Key 30 天有效；到期前在 Skill 页面点「续期」不用换 Key，过期后只能重建。"})


def cmd_auth_status(args):
    key, source = load_key(required=False)
    if not key:
        out({"configured": False, "get_key_at": KEY_PAGE})
        return
    masked = key[:8] + "…" + key[-4:]
    try:
        user = request("GET", "/bayuser/user", key=key) or {}
        out({"configured": True, "source": source, "key": masked, "valid": True,
             "nickname": user.get("nickname"), "user_id": user.get("id"),
             "summary": f"扇贝 API Key 现在有效（账号：{user.get('nickname')}）。具体哪天到期这里查不到，请到 {KEY_PAGE} 看；"
                        f"Key 30 天有效，没过期之前可以在页面上点「续期」（Key 不变），过期了就只能点「重建」换新 Key。"})
    except ApiError as e:
        if e.status == 429:   # 网关先验 Key 再限流，被限流说明 Key 是有效的
            out({"configured": True, "source": source, "key": masked, "valid": True,
                 "rate_limited": "Key 有效，但这个账号现在被扇贝限流（调用太频繁），过一会儿再用。"})
            return
        unreachable = e.status == 0 or (e.status == 404 and not e.body) or e.status >= 500
        out({"configured": True, "source": source, "key": masked,
             "valid": None if unreachable else False, "reason": explain(e)})


def cmd_me(args):
    u = call("GET", "/bayuser/user") or {}
    out({"user_id": u.get("id"), "nickname": u.get("nickname"), "username": u.get("username")})


# ---------------------------------------------------------------- 命令：读取

def current_book():
    """用户在扇贝里设的「当前在学」的书。拿不到时返回 (None, 原因)，不报错：
    没设当前书是 404 带原话；接口还没对 OpenAPI 开放是空 body 的 404。"""
    try:
        d = call("GET", "/wordsapp/user_material_books/current") or {}
    except ApiError as e:
        if e.status in (0, 401, 429) or e.status >= 500:   # 连不上、Key 不对、限流、服务出错：照常报错，不去查书桌
            raise
        if e.status == 404 and e.body:
            return None, "用户在扇贝里还没设当前在学的书"
        return None, "取不到当前在学的书" + ("（接口还没开放）" if e.status == 404 else f"（{e.status} {e.msg}）")
    mb = d.get("materialbook") or {}
    book_id = d.get("materialbook_id") or mb.get("id")
    if not book_id:
        return None, "取不到当前在学的书"
    return {"book_id": book_id, "name": mb.get("name"), "is_custom": bool(mb.get("is_custom")),
            "total": d.get("total_count", mb.get("total_count")), "finished": d.get("finished_count"),
            "daily_new": d.get("new_count"), "daily_review": d.get("review_count")}, None


def match_name(rows, name, key="name"):
    """用户常用泛称（「考研词汇」），书名是「完全版考研考纲词汇」：先按子串匹配（exact），
    再按「关键词的字按顺序出现在书名里」模糊匹配（fuzzy），两类都返回。"""
    kw = re.sub(r"\s+", "", (name or "").lower())
    exact, fuzzy = [], []
    for r in rows:
        nm = re.sub(r"\s+", "", (r.get(key) or "").lower())
        if kw and kw in nm:
            exact.append({**r, "match": "exact"})
        elif kw:
            it = iter(nm)
            if all(ch in it for ch in kw):
                fuzzy.append({**r, "match": "fuzzy"})
    return exact + fuzzy


def _desk_row(o):
    mb = o.get("materialbook") or {}
    return {"book_id": o.get("materialbook_id") or mb.get("id"), "name": mb.get("name"),
            "finished": o.get("finished_count"), "total": o.get("total_count", mb.get("total_count")),
            "daily_new": o.get("new_count"), "daily_review": o.get("review_count")}


def book_by_name(name):
    """在书桌上按书名找书：只对上一本就返回 (book_id, 完整书名)；对上多本或一本都没有就打印说明并退出，让 AI 去问用户。
    也认 book_id（问过用户、选定一本后用 book_id 再跑同一条命令）。"""
    cached = _cached("desk", 180) is not None
    rows = [_desk_row(o) for o in desk_books(recent_ok=True)]
    hits = [r for r in rows if r["book_id"] == name.strip()] or match_name(rows, name)
    if not hits and cached:   # 缓存里没有：用户可能刚在网页上把书加到书桌，现取一次
        rows = [_desk_row(o) for o in desk_books()]
        hits = [r for r in rows if r["book_id"] == name.strip()] or match_name(rows, name)
    if len(hits) == 1:
        return hits[0]["book_id"], hits[0]["name"]
    if not hits:   # 书桌上没有：多半是自己建了还没加到书桌的书，给出加书桌的链接
        books = all_custom_books()
        mine = [b for b in books if b.get("id") == name.strip()] or match_name(books, name)
        if len(mine) == 1:
            url = WEB_BOOK_URL.format(book_id=mine[0].get("id"))
            out({"ok": False, "book": mine[0].get("name"), "on_desk": False, "open_in_web": url,
                 "summary": f"「{mine[0].get('name')}」是你自己建的书，还没加到书桌，扇贝只认书桌上的书。"
                            f"请打开 {url} 把它加到书桌（设好每天背多少），加好之后再说一次。",
                 "next": "照 summary 告诉用户，现在结束这一轮。不要再试别的办法。"})
            sys.exit(4)
    if hits:
        out({"ok": False, "ask_user": True, "reason": f"书桌上有 {len(hits)} 本书对得上「{name}」",
             "candidates": [{k: h[k] for k in ("book_id", "name", "finished", "total", "daily_new", "daily_review")}
                            for h in hits],
             "next": "不要自己挑。现在结束这一轮，在回复里把这几本列出来问用户是哪一本，每本带上已学 / 总词数和每天的新词、复习量，"
                     "等他回复。他选定后用那本的 book_id 运行同一条命令。"})
    else:
        out({"ok": False, "ask_user": True, "reason": f"书桌上没有对得上「{name}」的书",
             "desk_books": [r["name"] for r in rows][:50],
             "next": "告诉用户书桌上没找到这本，问他说的是哪一本（可以举几个书桌上的书名），等他回复。"})
    sys.exit(4)


def resolve_book(book_id, name=None):
    """progress / today：给了 book_id 就用它；给了 --name 就在书桌上按书名找；都没给就用当前在学的书。"""
    if book_id:
        return book_id, None
    if name:
        return book_by_name(name)
    cur, why = current_book()
    if not cur:
        # 顺手把书桌列出来，省得 AI 再跑一次 books（那样会把当前书接口再撞一次 404）
        books = [{k: r[k] for k in ("book_id", "name", "finished", "total")} for r in map(_desk_row, desk_books())]
        few = len(books) <= 2
        out({"ok": False, "reason": f"没指定单词书，{why}", "desk_total": len(books), "desk_books": books,
             "next": ("书桌上只有这一两本：直接用其中的 book_id 再运行一次同一条命令，不用问用户。" if few else
                      "书桌上书很多：不要自己挑，也不要把列表念给用户。现在结束这一轮，问用户「你今天背的是哪本？说个大概的书名就行」，"
                      "等他回复。他说了书名就从 desk_books 里对上 book_id，带上 book_id 运行同一条命令，不用再跑 books。")})
        sys.exit(4)
    return cur["book_id"], cur["name"]


def cmd_set_current(args):
    """W6 换当前在学的书（PUT /wordsapp/user_material_books/current）。只能换成书桌上的书。"""
    go = consent(args)
    name = " ".join(args.name).strip()
    if not name:
        fail("要说换成哪本书。", code=4)
    cur, _ = current_book()
    book_id, full = book_by_name(name)
    old = cur["name"] if cur else None
    if cur and cur["book_id"] == book_id:
        out({"ok": True, "unchanged": True, "book": full,
             "summary": f"「{full}」本来就是当前在学的书，不用换。", "next": "照 summary 告诉用户，什么都不用写。"})
        return
    change = (f"把当前在学的书从「{old}」换成「{full}」" if old else f"把「{full}」设成当前在学的书")
    if not go:
        preview("换在学的书", book=full, current=old, summary=change + "。换了以后扇贝 App 打开就是这本，"
                "每天背多少、学习进度都跟着这本书走；原来那本还在书桌上，进度不会丢。")
        return
    if blocked("set-current", {"book": full, "previous": old, "summary": f"已{change}。扇贝 App 打开就是这本；原来那本还在书桌上，进度不会丢。"},
               book_id=book_id, book=full):
        return
    try:
        call("PUT", "/wordsapp/user_material_books/current", body={"materialbook_id": book_id})
    except ApiError as e:
        if e.status in (400, 403, 404) and e.body:
            fail(f"扇贝没让换：{e.msg}。不要再试，把这句原话告诉用户；书桌满了的话，请他在扇贝 App 里先移走不背的书。", code=5)
        raise
    out({"ok": True, "book": full, "previous": old,
         "summary": f"已{change}。扇贝 App 打开就是这本；原来那本还在书桌上，进度不会丢。"})


def cmd_books(args):
    """R9 书桌上的单词书。"""
    cur, cur_why = current_book()
    rows = []
    for o in desk_books():
        mb = o.get("materialbook") or {}
        rows.append({
            "book_id": o.get("materialbook_id") or mb.get("id"),
            "name": mb.get("name"),
            "is_custom": bool(mb.get("is_custom")),
            "total": o.get("total_count", mb.get("total_count")),
            "finished": o.get("finished_count"),
            "daily_new": o.get("new_count"),
            "daily_review": o.get("review_count"),
            "_updated": (o.get("updated_at") or "")[:10],   # 只用来排序，不输出：模型会拿它推「最近在背」
            "all_learned": o.get("status") == 1,
            "is_current": bool(cur) and (o.get("materialbook_id") or mb.get("id")) == cur["book_id"],
        })
    rows.sort(key=lambda r: (r["is_current"], r["_updated"]), reverse=True)
    for r in rows:
        r.pop("_updated", None)
    if args.name:
        rows = match_name(rows, args.name)
    total = len(rows)
    if args.limit:
        rows = rows[: args.limit]
    cur_row = next((r for r in rows if r["is_current"]), None)
    lead = (f"书桌上一共 {total} 本书" if not args.name else f"书桌上对得上「{args.name}」的有 {total} 本") + \
           (f"，这次列出 {len(rows)} 本" if len(rows) < total else "") + "。"
    if cur_row:
        lead += (f"当前在学的是「{cur_row['name']}」（已学 {cur_row['finished']} / {cur_row['total']} 个词，"
                 f"每天 {cur_row['daily_new']} 个新词、{cur_row['daily_review']} 个复习）。")
    done = sum(1 for r in rows if r["all_learned"])
    if done:
        lead += f"其中 {done} 本已经整本学过，之后每天只有复习词。"
    out({"total": total, "shown": len(rows), "current_book_id": cur["book_id"] if cur else None,
         "summary": lead,
         **({"current_note": cur_why} if cur_why else {}),
         "books": rows,
         "note": "排在最前、标了当前在学的那本，是用户在扇贝里设的正在背的书；其余的书接口看不出最近有没有在背，"
                 "不要说哪本「最近在背」。整本都学过的书，之后每天只有复习词。回复时用书名和数字说，不要提这里的英文字段名。"})


def _progress(book_id):
    """R6。当天第一次调用会为这本书生成今天的任务（和在 App 里打开这本书一样），生成期间回 412，脚本自动等。"""
    s = call("GET", f"/wordsapp/user_material_books/{book_id}/learning/statuses", retry_412=6) or {}
    return {
        "date": s.get("date"),
        "new": {"total": s.get("a_count"), "done": s.get("a_finished_count")},
        "review": {"total": s.get("c_count"), "done": s.get("c_finished_count")},
        "is_finished": s.get("is_finished"),
        "turn_count": s.get("turn_count"),
        "can_start_another_turn": s.get("can_init_next_turn"),
        "learning_seconds": s.get("learning_time"),
    }


def _mins(secs):
    secs = secs or 0
    return "不到 1 分钟" if 0 < secs < 60 else f"{round(secs / 60)} 分钟"


def progress_summary(name, p):
    """把进度拼成一句能直接转述的话，省得 AI 自己从字段里算「还剩几个」、猜「再来一轮」。"""
    n, r = p["new"], p["review"]
    nt, nd, rt, rd = (n["total"] or 0), (n["done"] or 0), (r["total"] or 0), (r["done"] or 0)
    head = f"「{name}」今天" if name else "这本书今天"
    if nd == 0 and rd == 0:
        s = f"{head}还没开始背：要背 {nt} 个新词、{rt} 个复习词。"
    else:
        s = (f"{head}新词 {nt} 个背了 {nd} 个" + (f"（还剩 {nt - nd} 个）" if nt > nd else "") +
             f"，复习词 {rt} 个背了 {rd} 个" + (f"（还剩 {rt - rd} 个）" if rt > rd else "") +
             f"，学了 {_mins(p['learning_seconds'])}。")
        if p["is_finished"]:
            s += "今天这一轮已经背完了。"
            if p["can_start_another_turn"]:
                s += "想多背可以在扇贝 App 里点「再来一轮」。"
        else:
            s += "这一轮还没背完。"
    return s


def cmd_progress(args):
    """R6 今天的学习进度。"""
    book_id, name = resolve_book(args.book_id, getattr(args, "name", None))
    p = _progress(book_id)
    out({"book_id": book_id, **({"book": name} if name else {}), **p,
         "summary": progress_summary(name, p),
         "note": "回复用户时按 summary 的意思说（用用户提问的语言），不要提英文字段名。"})


def cmd_today(args):
    """R7 今天任务里的单词，连同今天的进度一起给。"""
    book_id, name = resolve_book(args.book_id, getattr(args, "name", None))
    # 先调 R6：今天的任务没生成时 R7 会回 412，R6 会把任务生成好；进度顺带给出去，不用再单独跑 progress
    progress = _progress(book_id)
    q = {"ipp": 50, "type_of": args.type, "only_unknown": "true" if args.only_unknown else None}
    path = f"/bay-openapi/user_material_books/{book_id}/today_words"
    words, page, total = [], 1, None
    while True:
        q["page"] = page
        data = call("GET", path, q, retry_412=4) or {}
        objs = data.get("objects") or []
        total = data.get("total", len(objs))
        words.extend(objs)
        if not objs or len(words) >= total:
            break
        page += 1
        time.sleep(0.3)
    unknown_today = [w for w in words if (w.get("today_unknown_count") or 0) > 0]
    hard = [w for w in words if (w.get("unknown_count") or 0) >= 3]
    summary = progress_summary(name, progress)
    started = progress["new"]["done"] or progress["review"]["done"]
    if args.type or args.only_unknown:
        summary += f"这次列出的 {total} 个词：" + ("今天任务里的全部新词" if args.type == "NEW" else "今天任务里的全部复习词"
                                                if args.type == "REVIEW" else "今天点过「不认识」或累计 3 次以上的词")
        summary += "（今天已经背过的也在里面，接口不标哪个背过）。" if args.type and started else "。"
    elif not started:
        new = [w["word"] for w in words if w.get("type") == "NEW"]
        rev = [w["word"] for w in words if w.get("type") == "REVIEW"]
        summary += (f"今天要背的新词（{len(new)} 个）：{'、'.join(new)}。" if new else "") + \
                   (f"复习词（{len(rev)} 个）：{'、'.join(rev)}。" if rev else "")
    if unknown_today:
        summary += "今天点过「不认识」的：" + "、".join(
            f"{w['word']}（{w['today_unknown_count']} 次）" for w in unknown_today) + "。"
    elif not args.type and started:
        summary += "今天没有点过「不认识」的词。"
    if hard:
        summary += "老是记不住的（累计 3 次以上）：" + "、".join(f"{w['word']}（累计 {w['unknown_count']} 次）" for w in hard) + "。"
    out({"book_id": book_id, **({"book": name} if name else {}),
         "progress": progress, "total": total, "words": words, "summary": summary,
         "note": "words 是今天任务里的全部词，背完的也在里面——接口不标哪个词已经背过，所以不要说哪几个词还没背，"
                 "只说还剩几个（见 summary）。type 是 NEW 新词 / REVIEW 复习词；today_unknown_count 是今天点「不认识」的次数，"
                 "unknown_count 是累计次数（累计 3 次以上才有值）。回复按 summary 的意思说（用用户提问的语言），"
                 "要列词就列 words 里的，不要提英文字段名。"})


def cmd_custom_books(args):
    """R3 自建的自定义单词书，并标出是否在书桌上（R4 只能读书桌上的）。"""
    books = all_custom_books()
    on_desk = {o.get("materialbook_id") for o in desk_books()}
    rows = [{"book_id": b.get("id"), "name": b.get("name"), "total": b.get("total_count"),
             "tags": [t.get("name") for t in b.get("tag") or []],
             "on_desk": b.get("id") in on_desk,
             "open_in_web": WEB_BOOK_URL.format(book_id=b.get("id"))} for b in books]
    fmt = lambda rs: "、".join(f"「{r['name']}」{r['total']} 个词" for r in rs)
    yes, no = [r for r in rows if r["on_desk"]], [r for r in rows if not r["on_desk"]]
    if not rows:
        summary = "你还没有建过单词书。"
    else:
        summary = f"你建过 {len(rows)} 本单词书（按创建先后排，最后一本最新）。"
        summary += f"在书桌上的 {len(yes)} 本：{fmt(yes)}。" if yes else "都还没加到书桌。"
        if yes and no:
            summary += f"还没加到书桌的 {len(no)} 本：{fmt(no)}。"
        if no:
            summary += "没加到书桌的书读不出里面的词，要先打开它的网页链接加到书桌。"
    out({"total": len(rows), "on_desk": len(yes), "not_on_desk": len(no), "summary": summary, "books": rows,
         "note": "回复照 summary 说，本数、词数都以 summary 为准，不要自己另数。"
                 "列表里可能有已经删除的书（接口分不出来），回复里如实说一句。不要提英文字段名。"})


def custom_book(ref, books=None):
    """按 book_id 或书名找自己建的单词书：对上一本返回它；对上多本或没有就打印说明并退出，让 AI 去问用户。"""
    books = all_custom_books() if books is None else books
    for b in books:
        if b.get("id") == ref:
            return b
    hits = match_name(books, ref)
    if len(hits) == 1:
        return hits[0]
    if hits:
        out({"ok": False, "ask_user": True, "reason": f"有 {len(hits)} 本自建的书对得上「{ref}」",
             "candidates": [{"book_id": h.get("id"), "name": h.get("name"), "total": h.get("total_count")} for h in hits],
             "next": "不要自己挑。结束这一轮，把这几本（书名、词数）列给用户，问是哪一本，等他回复后用那本的 book_id 再运行。"})
    else:
        out({"ok": False, "ask_user": True, "reason": f"没找到叫「{ref}」的自建单词书",
             "my_books": [b.get("name") for b in books][:50],
             "next": "告诉用户没找到这本书，把他建过的书名列出来问是哪本，等他回复。"})
    sys.exit(4)


def cmd_custom_words(args):
    """R4 自定义单词书里的单词（只认书桌上的书）。先查书在不在书桌上，不在就不去读（读了也是 404）。"""
    b = custom_book(args.book_id)
    if b.get("id") not in {o.get("materialbook_id") for o in desk_books()}:
        url = WEB_BOOK_URL.format(book_id=b.get("id"))
        out({"ok": False, "book": b.get("name"), "total": b.get("total_count"), "on_desk": False,
             "open_in_web": url,
             "summary": f"「{b.get('name')}」还不在书桌上，扇贝只让读书桌上的书里的词。请用户打开 {url} 把它加到书桌"
                        f"（设好每天背多少），加好之后再来读。",
             "next": "照 summary 告诉用户，不要再试别的办法去读。"})
        return
    args.book_id = b.get("id")
    path = f"/bay-openapi/custom_books/{args.book_id}/words"
    words, page, total = [], 1, None
    while True:
        try:
            data = call("GET", path, {"ipp": 100, "page": page}) or {}
        except ApiError as e:
            if e.status == 404:
                fail(f"读不到这本书的单词：它不存在，或者不在用户的书桌上（{e.msg}）。"
                     f"OpenAPI 只能读书桌上的自定义单词书，请用户在扇贝里把它加到书桌：{WEB_BOOKS_URL}", code=2)
            raise
        objs = data.get("objects") or []
        total = data.get("total", len(objs))
        words.extend(objs)
        if not args.all or not objs or len(words) >= total:
            break
        page += 1
        time.sleep(0.6)  # 6000 词的书要翻 60 页，放慢一点，别撞限流
    out({"book_id": args.book_id, "book": b.get("name"), "total": total, "returned": len(words),
         "words": [{"word": w.get("word"), "vocab_id": w.get("id"), "learned": w.get("learned")} for w in words],
         **({} if args.all or len(words) >= (total or 0) else {"more": "只取了前 100 个，加 --all 取全部"})})


def cmd_collections(args):
    """R1 生词本列表。"""
    out({"books": [{
        "book_type": b.get("book_type"), "name": b.get("book_name"),
        "total": b.get("total_count"), "learned": b.get("learned_count"),
        "is_default": b.get("book_type") == 0,
    } for b in collection_books()],
        "note": "后续命令用 book_type 指定生词本；book_type=0 是默认生词本。"})


def cmd_collection_words(args):
    """R2 生词本里的单词（lite 模式，每页最多 1500 个，按加入时间从新到旧）。"""
    book = find_collection(args.book_type)
    rows, page, total = [], 1, None
    while True:
        data = call("GET", "/wordscollection/words",
                    {"book_type": book.get("book_type"), "lite": "true", "ipp": 1500, "page": page}) or {}
        objs = data.get("objects") or []
        total = data.get("total", len(objs))
        for o in objs:
            v = o.get("vocabulary") or {}
            meta = o.get("objects") or {}
            rows.append({"word": v.get("word"), "vocab_id": v.get("id") or meta.get("vocab_id"),
                         "added_at": (meta.get("created_at") or "")[:10]})
        if not objs or len(rows) >= total:
            break
        page += 1
    result = {"book": book.get("book_name"), "book_type": book.get("book_type"), "total": total}
    if args.word or args.word_file:
        want = set()
        if args.word:
            # 有逗号等分隔符时按分隔符拆（词组里的空格保留），只有空格时才按空格拆
            sep = r"[,，、;；\n]+" if re.search(r"[,，、;；\n]", args.word) else r"\s+"
            want = {re.sub(r"\s+", " ", w.strip().lower()) for w in re.split(sep, args.word) if w.strip()}
        if args.word_file:
            want |= {w.lower() for w in read_words(argparse.Namespace(words=[], file=args.word_file))}
        found = {r["word"].lower(): r for r in rows if (r["word"] or "").lower() in want}
        result["in_book"] = [found[w] for w in sorted(found)]
        result["not_in_book"] = sorted(want - set(found))
    else:
        if args.since:
            rows = [r for r in rows if r["added_at"] >= args.since]
            result["since"] = args.since
        result["matched"] = len(rows)
        if not args.all and len(rows) > args.limit:
            result["more"] = f"只列了最近加入的 {args.limit} 个，加 --all 列全部，或用 --limit 调整"
            rows = rows[: args.limit]
        result["words"] = rows
        by_day = {}
        for r in rows:
            by_day.setdefault(r["added_at"] or "日期不明", []).append(r["word"])
        cut = "more" in result   # 列表被截断时，最后一天可能还有更早加的没列出来
        days = list(by_day.items())
        parts = [f"{d} {'列出 ' if cut and i == len(days) - 1 else ''}{len(ws)} 个"
                 f"{'（这天还有没列出的）' if cut and i == len(days) - 1 else ''}（{'、'.join(ws)}）"
                 for i, (d, ws) in enumerate(days)]
        result["summary"] = (f"「{book.get('book_name')}」一共 {total} 个词，下面是最近加入的 {len(rows)} 个，按加入日期："
                             + "；".join(parts) + "。")
        result["note"] = "要按日期说，就用 summary 里的分组和个数，不要自己重新数。"
    out(result)


def cmd_tags(args):
    """R8 建自定义单词书时可选的分类。"""
    data = call("GET", "/wordsapp/tags") or {}
    out({"tags": [{"tag_id": t.get("id"), "name": t.get("name")}
                  for t in data.get("objects") or [] if t.get("status", 1) == 1]})


def cmd_lookup(args):
    """按拼写查单词 id（只返回 id，不返回释义）。"""
    out(lookup_vocab(args.word))


def cmd_notes(args):
    """R5 用户自己写的单词笔记。"""
    if args.word:
        v = lookup_vocab(args.word)
        notes = own_notes(v["vocab_id"])
        out({"word": v["word"], "vocab_id": v["vocab_id"], "notes": [{
            "note_id": n.get("id"), "content": (n.get("vocab_note") or {}).get("content")} for n in notes]})
        return
    notes, page, total = [], args.page, None
    while True:
        data = call("GET", "/wordsapp/user_vocab_notes", {"note_type": 1, "ipp": 20, "page": page}) or {}
        objs = data.get("objects") or []
        total = data.get("total", len(objs))
        notes.extend(objs)
        if not args.all or not objs or len(notes) >= total:
            break
        page += 1
    # 笔记接口只给 vocab_id。开放接口里没有「按 id 查拼写」，拿生词本里的词做一次对照，能补上一部分。
    id2word = {}
    try:
        for b in collection_books():
            d = call("GET", "/wordscollection/words",
                     {"book_type": b.get("book_type"), "lite": "true", "ipp": 1500, "page": 1}) or {}
            for o in d.get("objects") or []:
                v = o.get("vocabulary") or {}
                if v.get("id"):
                    id2word[v["id"]] = v.get("word")
    except ApiError:
        pass
    rows = []
    for n in notes:
        vn = n.get("vocab_note") or {}
        rows.append({"note_id": n.get("id"), "vocab_id": vn.get("vocab_id"),
                     "word": id2word.get(vn.get("vocab_id")), "content": vn.get("content")})
    unknown = sum(1 for r in rows if not r["word"])
    out({"total": total, "returned": len(rows), "notes": rows,
         **({} if args.all or len(rows) >= (total or 0) else {"more": "每页 20 条，加 --all 取全部，或用 --page 翻页"}),
         "summary": f"一共 {total} 条笔记，这次取到 {len(rows)} 条；其中 {len(rows) - unknown} 条能对上是哪个词，"
                    f"{unknown} 条接口没给拼写、生词本里也没有。",
         "note": "按写笔记的时间从新到旧。word 为 null 的：从笔记内容判断是哪个词，判断不出就说「这条看不出是哪个词」，"
                 "不要逐个去猜着查，也不要编。数条数时以这里的数字为准。"})


# ---------------------------------------------------------------- 命令：写入

def cmd_create_collection(args):
    """W1 新建生词本。"""
    name = args.name.strip()
    if not name:
        fail("生词本名称不能为空。", code=4)
    go = consent(args)
    if not go:
        preview("新建生词本", name=name, rule="非会员只能自建 1 本，会员上限更高；超出时扇贝会拒绝。")
        return
    if blocked("create-collection", name=name):
        return
    try:
        b = call("POST", "/wordscollection/user_words_collection_books", body={"book_name": name}) or {}
    except ApiError as e:
        if e.status in (400, 403):
            fail(f"扇贝没让建这个生词本：{e.msg or '超出上限'}。不要再试。告诉用户这句原话，再用 collections 列出他现有的"
                 f"生词本和词数，建议往已有的里加，或者到扇贝里删掉不用的再建。", code=5)
        raise
    out({"ok": True, "name": b.get("book_name", name), "book_type": b.get("book_type"),
         "summary": f"已新建生词本「{b.get('book_name', name)}」。生词本名要过审核，审核期间名字后面会带「（审核中）」。"})


def collection_word_set(bt):
    """生词本里已有的词（小写），lite 模式每页 1500 个。"""
    have, page = set(), 1
    while True:
        d = call("GET", "/wordscollection/words", {"book_type": bt, "lite": "true", "ipp": 1500, "page": page}) or {}
        objs = d.get("objects") or []
        for o in objs:
            w = ((o.get("vocabulary") or {}).get("word") or "").strip().lower()
            if w:
                have.add(w)
        if not objs or page * 1500 >= (d.get("total") or 0):
            break
        page += 1
    return have


def cmd_add_to_collection(args):
    """W2 往生词本加词（异步，自动等结果）。已经在生词本里的词自动跳过，不重复提交。"""
    asked = read_words(args)
    go = consent(args)   # 先查同意，看不出同意就别白调接口
    book = find_collection(args.book_type)
    bt = book.get("book_type")
    have = collection_word_set(bt)
    already = [w for w in asked if w.lower() in have]
    words = [w for w in asked if w.lower() not in have]
    name = book.get("book_name")
    skip_note = f"{'、'.join(already)} 本来就在「{name}」里，不重复加。" if already else ""
    if not words:
        out({"ok": True, "written": False, "book": name, "already_in_book": already, "added": 0,
             "summary": f"这些词本来就都在「{name}」里，没有重复提交。"})
        return
    if not go:
        preview("往生词本加词", book=name, book_type=bt, count=len(words), words=words, already_in_book=already,
                summary=f"要往「{name}」加 {len(words)} 个词：{'、'.join(words[:30])}{' 等' if len(words) > 30 else ''}。"
                        + skip_note)
        return
    if blocked("add-to-collection", {"book": name, "submitted": len(words), "added": len(words),
                                      "already_in_book": already, "failed_words": [],
                                      "summary": f"已往「{name}」加了 {len(words)} 个词。{skip_note}",
                                      "open_in_web": WEB_COLLECTION_URL},
               book_type=bt, words=words):
        return
    def submit(ws):
        return call("POST", "/wordscollection/words_bulk_upload", {"book_type": bt},
                    body={"business_id": WORDS_BUSINESS_ID, "words": ws}) or {}

    later, cap_note = [], ""
    try:
        res = submit(words)
    except ApiError as e:
        if not (e.status == 429 and not e.rate_limited):
            raise
        # 每日加词上限：整批被拒、什么都没加。用户已经同意加这一批，就先加今天额度内的，剩下的列出来明天再加
        cap = _daily_cap(e.msg)
        if not cap or cap[2] <= 0:
            fail((f"扇贝每天最多往生词本加 {cap[0]} 个词，今天已经加满了（{cap[1]} 个），这次一个都没加进去。"
                  if cap else f"扇贝拒绝了这次加词：{e.msg or '超出每天的加词上限'}。这次一个都没加进去。")
                 + "北京时间 0 点后再加。不要现在重试。", code=5)
        limit, used, left = cap
        later, words = words[left:], words[:left]
        cap_note = (f"扇贝每天最多往生词本加 {limit} 个词，今天之前已经加了 {used} 个，所以这次只加了前 {left} 个；"
                    f"剩下的 {len(later)} 个明天（北京时间 0 点后）再加。")
        res = submit(words)
    task_id = res.get("task_id")
    status = {}
    deadline = time.time() + 40
    try:
        while time.time() < deadline:
            time.sleep(2)
            status = call("GET", "/wordscollection/words_bulk_upload",
                          {"task_id": task_id, "business_id": WORDS_BUSINESS_ID, "book_type": bt}) or {}
            if status.get("status") in (4, 5):
                break
    except ApiError as e:
        out({"ok": None, "submitted": len(words), "book": name, "later_words": later,
             "summary": f"词已经提交给扇贝、在后台加，查结果时出错（{_brief(e)}）。过一会儿到生词本里看有没有加进去。" + cap_note,
             "next": "不要重新提交。", "open_in_web": WEB_COLLECTION_URL})
        return
    st = status.get("status")
    if st == 4:
        failed = [w for w in re.split(r"[,\s]+", status.get("failed_words") or "") if w]
        added = (status.get("total_count") or len(words)) - (status.get("failed_count") or 0)
        out({"ok": True, "book": name, "submitted": len(words), "added": added, "added_words": words,
             "already_in_book": already, "failed_words": failed, "later_words": later,
             "summary": f"已往「{name}」加了 {added} 个词。" + skip_note
                        + (f"扇贝词库没收录这些，没加进去：{'、'.join(failed)}。" if failed else "") + cap_note,
             "open_in_web": WEB_COLLECTION_URL,
             "note": "回复按 summary 说；有 later_words 就把它们列给用户（太多就说总数、举几个）。不要再提交一次。"})
    elif st == 5:
        fail(f"加词失败：{status.get('error_msg') or '扇贝没给原因'}")
    else:
        out({"ok": None, "book": name, "submitted": len(words), "later_words": later,
             "summary": "扇贝还在处理，40 秒内没出结果，稍后到生词本里看。" + cap_note,
             "next": "不要重复提交。", "open_in_web": WEB_COLLECTION_URL})


def _brief(e):
    if e.status == 429:
        return "被扇贝限流" if e.rate_limited else f"扇贝说「{e.msg}」"
    return f"{e.status} {e.msg}".strip() if e.status else f"网络中断：{e.msg}"


def _daily_cap(msg):
    """生词本批量加词的每日上限。扇贝原话形如「每天最多批量上传6000个单词，今日已上传5950个」。
    返回 (上限, 今天已加, 还能加)；认不出原话返回 None。"""
    m = re.search(r"最多批量上传\s*(\d+)\s*个单词.*?已上传\s*(\d+)", msg or "")
    if not m:
        return None
    limit, used = int(m.group(1)), int(m.group(2))
    return limit, used, max(0, limit - used)


def _poll_vocabs(task_id, timeout=120):  # 实测 600 个词要解析 20 秒左右
    deadline = time.time() + timeout
    while True:
        try:
            return call("GET", "/wordslearning/custom_books/vocabs", {"task_id": task_id}) or {}
        except ApiError as e:
            if e.status == 412 and time.time() < deadline:
                time.sleep(2)
                continue
            raise


INVALID_STOP = 0.3        # 识别不了的超过三成：建书 / 追加前先停下问用户（建好再补词要占当天唯一一次编辑）
PARSE_CACHE = pathlib.Path(tempfile.gettempdir()) / "shanbay-skill-parsed.json"
PARSE_TTL = 3600


def _parse_once(words):
    res = call("POST", "/wordslearning/custom_books/vocabs", body={"vocabs": words}) or {}
    task_id = res.get("task_id")
    r = _poll_vocabs(task_id)
    invalid = r.get("invalid_vocabs") or []
    repeat = r.get("repeat_vocabs") or []
    bad = {w.lower() for w in invalid + repeat}
    valid = [w for w in words if w.lower() not in bad]
    remember_task(task_id, valid)
    return {"task_id": task_id, "valid": valid, "invalid": invalid, "repeat": repeat}


def _parse_key(words):
    import hashlib
    return hashlib.sha1("\n".join(w.lower() for w in words).encode("utf-8")).hexdigest()


def _parse_cache(update=None):
    try:
        data = json.loads(PARSE_CACHE.read_text(encoding="utf-8")) if PARSE_CACHE.exists() else {}
    except (OSError, ValueError):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if now - v.get("ts", 0) < PARSE_TTL}
    if update:
        data.update(update)
        try:
            PARSE_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return data


def parse_words(words):
    """把词表交给扇贝解析（不改用户数据）。超过 1000 个分批解析。同一份词表 1 小时内复用上次的结果，
    预览和 --yes 之间不用再解析一遍。返回 submitted / valid / invalid / repeat / ratio / task_id（分批时为 None）。"""
    key = _parse_key(words)
    hit = _parse_cache().get(key)
    if hit:
        return hit
    if len(words) <= MAX_WORDS:
        r = _parse_once(words)
        info = {"valid": r["valid"], "invalid": r["invalid"], "repeat": r["repeat"], "task_id": r["task_id"]}
    else:
        info = {"valid": [], "invalid": [], "repeat": [], "task_id": None}
        for i in range(0, len(words), MAX_WORDS):
            r = _parse_once(words[i:i + MAX_WORDS])
            for k in ("valid", "invalid", "repeat"):
                info[k] += r[k]
    info.update({"ts": time.time(), "submitted": len(words),
                 "ratio": round(len(info["invalid"]) / max(1, len(words)), 3)})
    _parse_cache({key: info})
    return info


def ensure_task(words, info):
    """建书 / 追加要一个 task_id 对应「这次要写的词」。分批解析过的，把识别出的词合起来再解析一次。"""
    if info.get("task_id"):
        return info["task_id"]
    if len(info["valid"]) > MAX_WORDS:
        fail(f"识别出 {len(info['valid'])} 个词，超过一本书一次能收的 {MAX_WORDS} 个。要拆成几本："
             f"把词表按 {MAX_WORDS} 个一份分开，每份各建一本（书名加上「1」「2」区分）。这次什么都没建。", code=4)
    r = _parse_once(info["valid"])
    info["task_id"] = r["task_id"]
    _parse_cache({_parse_key(words): info})
    return r["task_id"]


def _invalid_text(info, limit=20):
    inv = info["invalid"]
    if not inv:
        return ""
    return (f"扇贝词库认不出 {len(inv)} 个：{'、'.join(inv[:limit])}{' 等' if len(inv) > limit else ''}"
            f"（不会加进去；要改拼写就改好再提交）。")


def _stop_if_many_invalid(args, info, what):
    """识别不了的超过三成，就算用户说了「直接…」也先停下问。用户看过之后还要做，才加 --accept-invalid。"""
    if info["ratio"] <= INVALID_STOP or getattr(args, "accept_invalid", False):
        return
    inv = info["invalid"]
    out({"ok": False, "written": False, "stop": True, "submitted": info["submitted"],
         "valid_count": len(info["valid"]), "invalid_count": len(inv), "invalid_examples": inv[:20],
         "summary": f"提交的 {info['submitted']} 个词里，扇贝词库认不出 {len(inv)} 个（超过三成），"
                    f"比如：{'、'.join(inv[:12])}。能认出的有 {len(info['valid'])} 个。这次还没有{what}。",
         "next": f"停下，即使用户说过「直接…」也要先问：现在结束这一轮，把 summary 的情况告诉用户（数量和十来个例子），"
                 f"问他是只用能认出的 {len(info['valid'])} 个继续{what}，还是先改词表。用户说继续，再运行同一条命令并加上 "
                 f"--accept-invalid 和 --yes \"<他的原话>\"；用户改了词表就用新词表重新运行。"})
    sys.exit(4)


def cmd_check_words(args):
    """只解析词表、看扇贝认不认（不改用户数据）。建书 / 追加直接用 create-book / append-book 带词表，不用先跑这个。"""
    words = read_words(args, cap=False)
    info = parse_words(words)
    extra = {}
    if args.valid_out:
        pathlib.Path(args.valid_out).expanduser().write_text("\n".join(info["valid"]) + "\n", encoding="utf-8")
        extra["valid_words_file"] = args.valid_out
    elif len(info["valid"]) <= 200:
        extra["valid_words"] = info["valid"]
    out({**extra, "submitted": info["submitted"], "valid_count": len(info["valid"]),
         "invalid_words": info["invalid"], "repeat_words": info["repeat"],
         **({"task_id": info["task_id"]} if info.get("task_id") else {}),
         "summary": f"提交 {info['submitted']} 个词，扇贝认得 {len(info['valid'])} 个。" + _invalid_text(info),
         "note": "认不出的词照实告诉用户，不要自己改拼写重试。建书、追加直接用 create-book / append-book 带上词表。"})


def _resolve_tag(tag):
    tags = _cached("tags", 3600)
    if tags is None:
        data = call("GET", "/wordsapp/tags") or {}
        tags = [t for t in data.get("objects") or [] if t.get("status", 1) == 1]
        _store("tags", tags)
    if tag:
        for t in tags:
            if tag in (t.get("id"), t.get("name")):
                return t, tags
    return None, tags


def _words_brief(words, limit=200):
    return words if len(words) <= limit else words[:30]


def cmd_create_book(args):
    """W3 建自定义单词书：直接给词表（词或 --file），脚本负责解析、分批、检查识别率，再建书。"""
    name = (args.name or "").strip()
    if not name:
        fail("单词书名称不能为空：--name <书名>。", code=4)
    if len(name) > BOOK_NAME_MAX:
        fail(f"单词书名称最多 {BOOK_NAME_MAX} 个字，现在 {len(name)} 个。请缩短。", code=4)
    go = consent(args)
    tag, tags = _resolve_tag(args.tag)
    tag_names = "、".join(t.get("name") for t in tags)
    if args.tag and not tag:
        fail(f"分类「{args.tag}」不存在。只能从这些里选一个：{tag_names}。有对应考试的就选它，"
             f"没有就选最接近的（比如出国类考试选「留学」）。", code=4)
    if go and not tag:
        fail(f"建书要选一个分类：加 --tag <分类名>，这次什么都没建。可选：{tag_names}。有对应考试的就选它，"
             f"没有就选最接近的（比如出国类考试选「留学」）。", code=4)
    if args.task_id:   # 兼容：先跑过 check-words 拿到的 task_id
        valid = task_words(args.task_id) or []
        words, info = valid, {"valid": valid, "invalid": [], "repeat": [], "task_id": args.task_id,
                              "submitted": len(valid), "ratio": 0}
    else:
        words = read_words(args, cap=False)
        info = parse_words(words)
    _stop_if_many_invalid(args, info, "建书")
    n = len(info["valid"])
    if not go:
        preview("创建自定义单词书", name=name, tag=tag.get("name") if tag else None,
                **({} if tag else {"tag_options": [t.get("name") for t in tags]}),
                word_count=n, words=_words_brief(info["valid"]), invalid_words=info["invalid"][:30],
                summary=f"要建单词书「{name}」（分类：{tag.get('name') if tag else '还没选，从 tag_options 里选一个'}），"
                        f"共 {n} 个词。" + _invalid_text(info),
                rule="书名要过内容审核；新建的书只有自己能看到，要加到书桌才能背。")
        return
    if not tag:
        fail(f"建书要选一个分类：加 --tag <分类名>。可选：{tag_names}。", code=4)
    task_id = ensure_task(words, info)
    done = (f"已建好单词书「{name}」，共 {n} 个词。" + _invalid_text(info) +
            "新书不会自动出现在书桌上：打开下面的链接，设好每天背多少、加到书桌，就能开始背。"
            "书名要过审核，审核期间书名后面会带「(审核中)」。这本书只有你自己能看到。")
    if blocked("create-book", {"book_id": "evalbook", "name": name, "total": n,
                                "open_in_web": WEB_BOOK_URL.format(book_id="evalbook"), "summary": done},
               name=name, tag=tag.get("name"), words=info["valid"]):
        return
    b = call("POST", "/wordslearning/custom_books",
             body={"task_id": task_id, "name": name, "tag_ids": [tag.get("id")]}) or {}
    book_id = b.get("id")
    out({"ok": True, "book_id": book_id, "name": b.get("name", name), "total": b.get("total_count", n),
         "words": _words_brief(info["valid"]),
         "open_in_web": WEB_BOOK_URL.format(book_id=book_id) if book_id else WEB_BOOKS_URL, "summary": done,
         "note": "回复按 summary 说，带上链接；用户没看过词表的（说了直接建），把词表贴出来（多就写总数、举几个例子）。"})


def cmd_append_book(args):
    """W4 往自己建的自定义单词书追加单词（要单词会员；每天只能编辑 1 次）。书可以给 book_id 或书名，词直接给。"""
    if args.task_id:   # 兼容：先跑过 check-words 拿到的 task_id
        words = task_words(args.task_id) or []
        info = {"valid": words, "invalid": [], "repeat": [], "task_id": args.task_id, "submitted": len(words),
                "ratio": 0}
    else:
        words = read_words(args)
        info = None
    go = consent(args)
    with _WordLock("custom-books"):  # 前后对比单词书列表期间不能有别的追加插进来
        books = all_custom_books()
        # 书名带空格又没加引号（append-book hello world quixotic）：把后面的词拼回书名，能完整对上某本书名就按它算
        if not args.task_id and args.words:
            names = {(b.get("name") or "").strip().lower() for b in books}
            for k in range(len(args.words), 0, -1):
                cand = " ".join([args.book] + args.words[:k])
                if cand.lower() in names and args.book.lower() not in names:
                    args.book, args.words = cand, args.words[k:]
                    if not args.words and not args.file:
                        fail(f"书名是「{cand}」，但没给要加的词。", code=4)
                    words = read_words(args)
                    break
        book = custom_book(args.book, books)
        book_id = book.get("id")
        if info is None:
            info = parse_words(words)
        _stop_if_many_invalid(args, info, "追加")
        adding = info["valid"]
        if not adding:
            fail("这些词扇贝词库都认不出，没有可以加的。" + _invalid_text(info), code=4)
        rule = "要单词会员；每人每天只能编辑 1 次单词书。今天已经编辑过的话，扇贝不改原书，而是把原书的词加上这次的词另建一本新书。"
        if not go:
            preview("往自定义单词书追加单词", book=book.get("name"), current_total=book.get("total_count"),
                    adding=_words_brief(adding), adding_count=len(adding), invalid_words=info["invalid"][:30],
                    summary=f"要往「{book.get('name')}」（现在 {book.get('total_count')} 个词）加 {len(adding)} 个词。"
                            + _invalid_text(info), rule=rule)
            return
        task_id = ensure_task(words, info)
        if blocked("append-book", {"book_id": book_id, "same_book": True, "state": "done",
                                    "summary": f"已往「{book.get('name')}」加了 {len(adding)} 个词。今天这一次编辑已经用掉了。",
                                    "open_in_web": WEB_BOOK_URL.format(book_id=book_id)},
                   book=book.get("name"), words=adding):
            return
        # 当天第二次编辑时，扇贝会把原书的词加上这次的词另建一本新书（有时还会同时报错），
        # 所以前后对比单词书列表，发现多出来的书就如实报告，避免 AI 当成失败再提交一次。
        before = {x.get("id") for x in books}
        try:
            b = call("PUT", f"/wordslearning/custom_books/{book_id}", body={"task_id": task_id}) or {}
            err = None
        except ApiError as e:
            b, err = {}, e
        try:
            new_books = [x for x in all_custom_books() if x.get("id") not in before]
        except ApiError as e2:
            if err is not None and err.status == 429:
                raise err   # 追加请求本身就被拒了，什么都没改
            if err is None and b.get("id") in (None, book_id):
                new_books = []   # 改的是原书，只是没法再核对
            elif err is None:
                new_books = [b]  # PUT 直接返回了另一本书
            else:
                out({"ok": None, "book": book.get("name"),
                     "summary": f"追加请求已经发出，但事后核对单词书列表时出错（{_brief(e2)}），"
                                f"不确定扇贝是改了原书还是另建了一本。过一会儿到「我的单词书」里看。",
                     "next": "不要再提交一次。", "open_in_web": WEB_BOOKS_URL})
                return
    if new_books or (b.get("id") and b.get("id") != book_id):
        nb = new_books[0] if new_books else b
        url = WEB_BOOK_URL.format(book_id=nb.get("id"))
        out({"ok": True, "same_book": False, "book_id": nb.get("id"), "name": nb.get("name"),
             "total": nb.get("total_count"), "open_in_web": url,
             "summary": f"今天已经编辑过一次单词书，扇贝没有改原来的「{book.get('name')}」，而是把原书的词加上这次的 "
                        f"{len(adding)} 个词另建了一本新书「{nb.get('name')}」（{nb.get('total_count')} 个词）：{url} 。"
                        f"新书要打开链接加到书桌才能背；原书没变，不需要的话可以到扇贝里删掉。",
             "next": "照 summary 告诉用户。不要再提交一次，否则会再多出一本。"})
        return
    if err:
        raise err
    upd = b.get("update_task_id")
    state = "done"
    if upd:
        state = "pending"
        deadline = time.time() + 15
        try:
            while time.time() < deadline:
                time.sleep(2)
                s = call("GET", f"/wordslearning/custom_books/{book_id}/update_status",
                         {"update_task_id": upd}) or {}
                code = s.get("status")
                if code == 0:
                    state = "done"
                    break
                if code == 3:
                    state = "task_not_found"
                    break
        except ApiError:
            pass   # 已经提交，查进度出错就按「还在更新」报
    total = b.get("total_count")
    out({"ok": state == "done", "book_id": b.get("id", book_id), "name": b.get("name", book.get("name")),
         "total": total, "state": state, "added_words": _words_brief(adding),
         "summary": (f"已往「{book.get('name')}」加了 {len(adding)} 个词" + (f"，现在一共 {total} 个" if total else "")
                     + "。今天这一次编辑已经用掉了。" + _invalid_text(info)
                     + ("扇贝还在更新词表，稍后到单词书里看。" if state == "pending" else "")),
         "next": "不要重复提交。",
         "open_in_web": WEB_BOOK_URL.format(book_id=b.get("id", book_id))})


class _WordLock:
    """同一个词的笔记「读原文 → 拼接 → 写回」要串行，否则两个进程同时追加会互相覆盖（实测丢过内容）。"""

    def __init__(self, vocab_id):
        self.path = pathlib.Path(tempfile.gettempdir()) / f"shanbay-skill-note-{vocab_id}.lock"
        self.f = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        self.f = open(self.path, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()


def _compose_note(old, text, replace):
    return text if (replace or not old) else old.rstrip() + "\n\n" + text


def cmd_note(args):
    """W5 写单词笔记：默认把新内容接在原笔记末尾，--replace 才覆盖。"""
    text = (args.text or "").strip()
    if args.text_file:
        text = read_text_any(args.text_file).strip()
    if not text:
        fail("笔记内容不能为空。", code=4)
    go = consent(args)
    v = lookup_vocab(args.word)
    with _WordLock(v["vocab_id"]):
        notes = own_notes(v["vocab_id"])
        old = (notes[0].get("vocab_note") or {}).get("content", "") if notes else ""
        new = _compose_note(old, text, args.replace)
        if len(new) > NOTE_MAX:
            fail(f"笔记最多 {NOTE_MAX} 字，写进去后会有 {len(new)} 字。请精简新内容"
                 + ("，或者经用户同意后用 --replace 覆盖。" if old and not args.replace else "。"), code=4)
        mode = "新建" if not old else ("覆盖" if args.replace else "追加")
        if not go:
            if mode == "追加":
                preview("追加单词笔记", word=v["word"], old_content=old, appended=text, final_length=len(new))
            else:
                preview(f"{mode}单词笔记", word=v["word"], old_content=old or None, new_content=new)
            return
        w = v["word"]
        done = {"新建": f"已给 {w} 写了一条笔记。", "覆盖": f"已把 {w} 原来的笔记换成了新内容。",
                "追加": f"已把新内容接在 {w} 原来的笔记后面，原来的内容还在。"}[mode] + "笔记要过内容审核，审核通过前可能暂时看不到。"
        if blocked("note", {"word": v["word"], "mode": mode, "written_content": new, "summary": done},
                   word=v["word"], mode=mode, content=new):
            return
        if notes:
            call("PUT", f"/wordsapp/user_vocab_notes/{notes[0].get('id')}", body={"content": new})
        else:
            call("POST", "/wordsapp/user_vocab_notes",
                 body={"vocab_id": v["vocab_id"], "content": new, "content_type": 1})
        # 本机的锁管不到别的电脑：另一台机器同时追加会把这次的内容覆盖掉。
        # 写完读回来核对，内容丢了就在最新内容上重新追加，最多补两次。
        retried = 0
        unverified = None
        while mode != "覆盖" and retried < 2:
            time.sleep(1.0)
            try:
                cur_notes = own_notes(v["vocab_id"])
            except ApiError as e:
                unverified = _brief(e)
                break
            cur = (cur_notes[0].get("vocab_note") or {}).get("content", "") if cur_notes else ""
            if text in cur:
                break
            retried += 1
            new = _compose_note(cur, text, False)
            if len(new) > NOTE_MAX:
                fail(f"笔记被别处同时改过，加上这次的内容会超过 {NOTE_MAX} 字，没有写进去。请精简后再试。", code=4)
            if cur_notes:
                call("PUT", f"/wordsapp/user_vocab_notes/{cur_notes[0].get('id')}", body={"content": new})
            else:
                call("POST", "/wordsapp/user_vocab_notes",
                     body={"vocab_id": v["vocab_id"], "content": new, "content_type": 1})
    out({"ok": True, "word": v["word"], "mode": mode, "written_content": new,
         **({"note_merged": "笔记同时被别处改过，已在最新内容后面重新追加"} if retried else {}),
         **({"unverified": f"已经写进去了；写完回读核对时出错（{unverified}），没核对。不要重写这一条。"}
            if unverified else {}),
         "summary": done,
         "note": "回复里贴的笔记内容以 written_content 为准（这是扇贝里现在的全文），不要贴你自己另写的版本。"})


# ---------------------------------------------------------------- 命令：原始调用

def cmd_api(args):
    """直接调只读接口（脚本没封装的情况用）。写入只能走上面的专用命令。"""
    if "?" in args.path or "#" in args.path:
        fail("路径里不要带参数，参数用 --query k=v 传。", code=4)
    query = {}
    for kv in args.query or []:
        k, _, v = kv.partition("=")
        query[k] = v
    if any(urllib.parse.unquote(k).strip().lower() == "user_id" for k in query):
        fail("只能读写 API Key 主人自己的数据，不能指定别的用户。", code=4)
    out(call("GET", args.path, query or None))


# ---------------------------------------------------------------- 入口

def main():
    # Windows 控制台默认是 GBK / cp1252，中文会写不出去或变乱码；统一按 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="shanbay.py", description="扇贝 OpenAPI 命令行（给 AI 助手用）")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="配置 / 检查 API Key")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    s = asub.add_parser("set", help="校验并保存 API Key（传 - 从标准输入读）")
    s.add_argument("key")
    s.set_defaults(fn=cmd_auth_set)
    asub.add_parser("status", help="看 API Key 是否配置、是否有效").set_defaults(fn=cmd_auth_status)

    sub.add_parser("me", help="当前 API Key 对应的扇贝账号").set_defaults(fn=cmd_me)

    s = sub.add_parser("books", help="R9 书桌上的单词书")
    s.add_argument("--name", help="按书名筛选")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_books)

    s = sub.add_parser("set-current", help="W6 换当前在学的书（只能换成书桌上的书）")
    s.add_argument("name", nargs="+", metavar="书名", help="书名可以只说个大概，在书桌上找；对上多本会停下让你问用户")
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话", help="带上用户同意的原话才真的换；不带就是预览")
    s.set_defaults(fn=cmd_set_current)

    s = sub.add_parser("progress", help="R6 某本书今天的学习进度")
    s.add_argument("book_id", nargs="?", help="book_id；不给就用当前在学的书")
    s.add_argument("--name", help="用户说了书名就用它（比如 --name 考研词汇），脚本在书桌上找，对上多本会停下让你问用户")
    s.set_defaults(fn=cmd_progress)

    s = sub.add_parser("today", help="R7 某本书今天任务里的单词")
    s.add_argument("book_id", nargs="?", help="book_id；不给就用当前在学的书")
    s.add_argument("--name", help="用户说了书名就用它，同 progress --name")
    s.add_argument("--type", choices=["NEW", "REVIEW"], help="只看新词或复习词")
    s.add_argument("--only-unknown", action="store_true", help="只看点过「不认识」的词")
    s.set_defaults(fn=cmd_today)

    sub.add_parser("custom-books", help="R3 自建的自定义单词书").set_defaults(fn=cmd_custom_books)

    s = sub.add_parser("custom-words", help="R4 自定义单词书里的单词")
    s.add_argument("book_id", metavar="书名或book_id")
    s.add_argument("--all", action="store_true", help="翻完所有页（默认只取前 100 个）")
    s.set_defaults(fn=cmd_custom_words)

    sub.add_parser("collections", help="R1 生词本列表").set_defaults(fn=cmd_collections)

    s = sub.add_parser("collection-words", help="R2 生词本里的单词")
    s.add_argument("--book-type", default="0", help="生词本的 book_type 或名称，默认 0（默认生词本）")
    s.add_argument("--word", help="只看这些词在不在生词本里（逗号或空格分隔）")
    s.add_argument("--word-file", help="同 --word，词表放在文件里（一行一个），词多时用")
    s.add_argument("--since", help="只看这天（YYYY-MM-DD）以后加入的")
    s.add_argument("--limit", type=int, default=50, help="最多列几个，默认 50（最近加入的在前）")
    s.add_argument("--all", action="store_true", help="全部列出")
    s.set_defaults(fn=cmd_collection_words)

    sub.add_parser("tags", help="R8 自定义单词书的分类").set_defaults(fn=cmd_tags)

    s = sub.add_parser("lookup", help="按拼写查单词 id")
    s.add_argument("word")
    s.set_defaults(fn=cmd_lookup)

    s = sub.add_parser("notes", help="R5 自己写的单词笔记")
    s.add_argument("--word", help="只看这个词的笔记")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--all", action="store_true", help="翻完所有页")
    s.set_defaults(fn=cmd_notes)

    s = sub.add_parser("create-collection", help="W1 新建生词本")
    s.add_argument("name")
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话",
                   help="真的写入。后面带上用户同意时的原话（或请求里「直接…」那句），看不出同意就不写")
    s.set_defaults(fn=cmd_create_collection)

    s = sub.add_parser("add-to-collection", help="W2 往生词本加词")
    s.add_argument("words", nargs="*")
    s.add_argument("--file")
    s.add_argument("--book-type", default="0", help="生词本的 book_type 或名称，默认 0（默认生词本）")
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话",
                   help="真的写入。后面带上用户同意时的原话（或请求里「直接…」那句），看不出同意就不写")
    s.set_defaults(fn=cmd_add_to_collection)

    s = sub.add_parser("check-words", help="只看扇贝认不认这些词（建书 / 追加不用先跑它）")
    s.add_argument("words", nargs="*")
    s.add_argument("--file")
    s.add_argument("--valid-out", help="把识别出的词存到这个文件（一行一个），分批合并时用")
    s.set_defaults(fn=cmd_check_words)

    s = sub.add_parser("create-book", help="W3 建自定义单词书：直接给词表，脚本负责解析")
    s.add_argument("words", nargs="*")
    s.add_argument("--file", help="词表文件，一行一个；超过 1000 个也行，脚本会分批解析")
    s.add_argument("--name", required=True, help="书名，1–20 个字")
    s.add_argument("--tag", help="分类名；不知道有哪些就先不给，预览里会列出来")
    s.add_argument("--accept-invalid", action="store_true",
                   help="识别不了的超过三成时，用户看过情况、明确说还要建，才加")
    s.add_argument("--task-id", help=argparse.SUPPRESS)
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话",
                   help="真的写入。后面带上用户同意时的原话（或请求里「直接…」那句），看不出同意就不写")
    s.set_defaults(fn=cmd_create_book)

    s = sub.add_parser("append-book", help="W4 往自己建的单词书追加单词：给书名（或 book_id）和词表")
    s.add_argument("book", metavar="书名或book_id")
    s.add_argument("words", nargs="*")
    s.add_argument("--file", help="词表文件，一行一个")
    s.add_argument("--accept-invalid", action="store_true",
                   help="识别不了的超过三成时，用户看过情况、明确说还要加，才加")
    s.add_argument("--task-id", help=argparse.SUPPRESS)
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话",
                   help="真的写入。后面带上用户同意时的原话（或请求里「直接…」那句），看不出同意就不写")
    s.set_defaults(fn=cmd_append_book)

    s = sub.add_parser("note", help="W5 写单词笔记（默认追加）")
    s.add_argument("word")
    s.add_argument("--text")
    s.add_argument("--text-file")
    s.add_argument("--replace", action="store_true", help="覆盖原笔记（用户明确要求时才用）")
    s.add_argument("--yes", nargs="?", const="", metavar="用户原话",
                   help="真的写入。后面带上用户同意时的原话（或请求里「直接…」那句），看不出同意就不写")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("api", help="直接 GET 一个开放接口（只读）")
    s.add_argument("path", help="如 /wordsapp/tags")
    s.add_argument("--query", action="append", help="k=v，可重复")
    s.set_defaults(fn=cmd_api)

    args = p.parse_args()
    run(args.fn, args)


if __name__ == "__main__":
    main()
