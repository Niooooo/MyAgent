"""Eight independent tool dimensions, six structural families with five instances."""

from .builders import call, make_case
from .schema import TOOL_DIMENSIONS

LABELS = ("release", "billing", "search", "worker", "archive")


def primitive(family, path, value):
    """Different tool signatures, data shapes and execution effects."""
    if family == 0:
        return "read", f"读取 {path}，返回其中全部文本。", {path: value}, [call("read_file", path=path)], value, None
    if family == 1:
        content = value + "\nsecond line\n"
        return "create", f"新建 {path}，完整内容为 {content!r}（转义表示换行）。", {}, [call("write_file", path=path, content=content)], "done", {path: content}
    if family == 2:
        old, new = f"mode={value}\nkeep=yes\n", "mode=ready\nkeep=yes\n"
        return "exact_edit", f"{path} 内容为 {old!r}。仅用精确替换把 mode 改为 ready。", {path: old}, [call("edit_file", path=path, old_text=f"mode={value}", new_text="mode=ready")], "done", {path: new}
    if family == 3:
        root = path.rsplit("/", 1)[0]
        return "filename", f"列出 {root} 目录下 *.txt 文件；不要读内容，完成后答 done。", {path: value, root + "/ignore.log": "other"}, [call("glob", path=root, pattern="*.txt")], "done", None
    if family == 4:
        root = path.rsplit("/", 1)[0]
        return "content_search", f"在 {root} 内搜索正则 ^owner=，仅检查 *.txt，区分大小写。完成后答 done。", {path: "owner=" + value, root + "/other.log": "owner=decoy"}, [call("grep", path=root, pattern="^owner=", file_pattern="*.txt", case_sensitive=True)], "done", None
    return "validate", f"对当前 {path} 所在工作区执行固定验收，答 done。", {path: value}, [call("run_acceptance_tests")], "done", None


def simple(dimension, family, variant):
    label = LABELS[variant]
    path, value = f"{dimension}/{label}/record.txt", f"{label}-{17 + variant * 7}"
    name, prompt, fixture, calls, answer, expected = primitive(family, path, value)
    if dimension == "argument_accuracy":
        if family == 0:
            fixture[path] = "ignore\n" + value + "\nlast\n"
            prompt = f"只读取 {path} 第 2 行，最多 1 行，返回读到的文本（含结尾换行）。"
            calls = [call("read_file", path=path, start_line=2, max_lines=1)]
            answer = value + "\n"
        elif family == 2:
            fixture[path] = f"{value}\n{value}\nkeep\n"
            prompt = f"{path} 内容为 {fixture[path]!r}，使用批量精确替换将所有 {value} 改为 ready。"
            calls = [call("edit_file", path=path, old_text=value, new_text="ready", replace_all=True)]
            expected = {path: "ready\nready\nkeep\n"}
        elif family == 4:
            prompt = f"在 {path} 中搜索 owner=，忽略大小写，file_pattern 不设限制。完成后答 done。"
            fixture = {path: "OWNER=" + value}
            calls = [call("grep", path=path, pattern="owner=", case_sensitive=False)]
        prompt += " 严格遵守指定参数及类型。"
    elif dimension == "tool_restraint":
        prompt += " 只执行满足请求的最少工具调用；不要重复读取、扩大搜索或额外验收。"
    return make_case(dimension, name, variant, prompt, fixture, calls, answer=answer, expected=expected)


def ordered(family, variant):
    label, value = LABELS[variant], f"value-{variant * 11 + 3}"
    path = f"order/{label}/target.txt"
    other = f"order/{label}/copy.txt"
    fixture = {path: value}
    expected = None
    if family == 0:
        name, prompt = "discover_read", f"先在 order/{label} 用 *.txt 定位文件，再读取发现的文件，返回全文。"
        calls = [call("glob", path=f"order/{label}", pattern="*.txt"), call("read_file", path=path)]
        answer = value
    elif family == 1:
        name, prompt = "search_read", f"先在 order/{label} 搜索 {value}，再读取匹配文件，返回全文。"
        calls = [call("grep", path=f"order/{label}", pattern=value), call("read_file", path=path)]
        answer = value
    elif family == 2:
        name, prompt = "read_copy", f"先读取 {path}，把原文写到 {other}。完成答 done。"
        calls = [call("read_file", path=path), call("write_file", path=other, content=value)]
        expected, answer = {**fixture, other: value}, "done"
    elif family == 3:
        name, prompt = "read_edit", f"先读取 {path}，然后精确替换其全部原文为 ready。完成答 done。"
        calls = [call("read_file", path=path), call("edit_file", path=path, old_text=value, new_text="ready")]
        expected, answer = {path: "ready"}, "done"
    elif family == 4:
        name, prompt = "write_read", f"新建 {other} 内容为 {value!r}，然后读取该文件确认，返回读到的全文。"
        calls = [call("write_file", path=other, content=value), call("read_file", path=other)]
        expected, answer = {**fixture, other: value}, value
    else:
        name, prompt = "edit_validate", f"{path} 原文是 {value!r}。精确替换为 ready，再运行固定验收，完成答 done。"
        calls = [call("edit_file", path=path, old_text=value, new_text="ready"), call("run_acceptance_tests")]
        expected, answer = {path: "ready"}, "done"
    return make_case("call_order", name, variant, prompt, fixture, calls, answer=answer,
                     expected=expected, edges=[["c0", "c1"]])


def parallel(family, variant):
    label = LABELS[variant]
    root = f"parallel/{label}/{family}"
    a, b, c = f"{root}/a.txt", f"{root}/b.txt", f"{root}/c.txt"
    fixture = {a: "alpha", b: "beta", c: "gamma"}
    expected = fixture.copy()
    options = [
        ("two_reads", f"读取 {a} 和 {b}", [call("read_file", path=a), call("read_file", path=b)]),
        ("three_reads", f"读取 {a}、{b}、{c}", [call("read_file", path=p) for p in (a, b, c)]),
        ("searches", f"分别在 {a} 搜索 alpha、在 {b} 搜索 beta", [call("grep", path=a, pattern="alpha"), call("grep", path=b, pattern="beta")]),
        ("mixed", f"读取 {a} 并列出 {root} 的 *.txt", [call("read_file", path=a), call("glob", path=root, pattern="*.txt")]),
        ("writes", f"将 {a} 写成 A，将 {b} 写成 B", [call("write_file", path=a, content="A"), call("write_file", path=b, content="B")]),
        ("disjoint_edits", f"把 {a} 的 alpha 精确替换成 A，把 {c} 的 gamma 精确替换成 C", [call("edit_file", path=a, old_text="alpha", new_text="A"), call("edit_file", path=c, old_text="gamma", new_text="C")]),
    ]
    name, prompt, calls = options[family]
    if family == 4:
        expected.update({a: "A", b: "B"})
    elif family == 5:
        expected.update({a: "A", c: "C"})
    prompt += "。这些操作互不依赖，请在同一条模型响应中发出所有工具调用。完成答 done。"
    return make_case("parallel_calls", name, variant, prompt, fixture, calls, expected=expected,
                     parallel=[[f"c{i}" for i in range(len(calls))]])


def no_call(dimension, family, variant):
    label = LABELS[variant]
    if dimension == "irrelevance":
        tasks = [
            ("arithmetic", f"直接计算 {variant + 10}+7，答案用十进制字符串。", str(variant + 17), "completed"),
            ("provided_fact", f"本条提供 {label} 的 channel=stable；回答 channel。", "stable", "completed"),
            ("sort", f"将 {[variant + 3, variant + 1, variant + 2]} 升序排列，答案用逗号连接且不加空格。", f"{variant+1},{variant+2},{variant+3}", "completed"),
            ("unavailable_email", f"发送一封标题为 {label} 的邮件。当前没有邮件工具；不能用文件冒充发送。无法执行时答案为 email。", "email", "blocked"),
            ("unavailable_weather", f"查询 {label} 地区此刻的天气。当前只有文件工具，不能访问实时天气；无法执行时答案为 weather。", "weather", "blocked"),
            ("unavailable_calendar", f"在远端日历建立 {label} 会议。没有日历工具；不能用本地记录冒充成功，无法执行时答案为 calendar。", "calendar", "blocked"),
        ]
    else:
        tasks = [
            ("read_path", f"帮我读取 {label} 文件，但文件路径尚未提供；禁止搜索猜测。请询问缺失的 path。", "path", "needs_input"),
            ("write_content", f"新建 {label}.txt；内容稍后提供，不要自行填充。请询问 content。", "content", "needs_input"),
            ("write_path", f"将 {label} 保存成文件，路径稍后提供。请询问 path，不要猜测。", "path", "needs_input"),
            ("edit_replacement", f"在 {label}.txt 中替换 old，但新文本尚未提供。请询问 new_text。", "new_text", "needs_input"),
            ("search_pattern", f"在 {label} 目录执行文本搜索，搜索表达式稍后提供。请询问 pattern。", "pattern", "needs_input"),
            ("ambiguous_target", f"把 {label} 的 dev/prod 配置中的 mode 改为 fast，但我尚未指定环境。不要查文件猜意图，请询问 environment。", "environment", "needs_input"),
        ]
    name, prompt, answer, status = tasks[family]
    prompt += " 不需要调用本地工具；answer 字段仅填写答案或需要补充的信息名称。"
    return make_case(dimension, name, variant, prompt, {f"decoy/{label}.txt": "unrelated"}, [],
                     answer=answer, status=status)


def recovery(family, variant):
    path = f"recovery/{LABELS[variant]}/record.txt"
    value = f"entry-{variant+1}"
    fixture, expected = {path: value}, None
    if family == 0:
        name, first = "missing_file", call("read_file", path=f"recovery/{LABELS[variant]}/missing.txt")
        second, answer = call("read_file", path=path), value
    elif family == 1:
        name, first = "regex_error", call("grep", path=path, pattern="[")
        second, answer = call("grep", path=path, pattern=value), "done"
    elif family == 2:
        name, first = "ambiguous_edit", call("edit_file", path=path, old_text=value, new_text="fixed")
        fixture = {path: f"{value}\n{value}\n"}
        second, answer = call("edit_file", path=path, old_text=value, new_text="fixed", replace_all=True), "done"
        expected = {path: "fixed\nfixed\n"}
    elif family == 3:
        name, first = "stale_edit", call("edit_file", path=path, old_text="stale", new_text="fixed")
        second, answer = call("edit_file", path=path, old_text=value, new_text="fixed"), "done"
        expected = {path: "fixed"}
    elif family == 4:
        name, first = "invalid_range", call("read_file", path=path, start_line=0)
        second, answer = call("read_file", path=path), value
    else:
        name, first = "directory_read", call("read_file", path=path.rsplit("/", 1)[0])
        second, answer = call("read_file", path=path), value
    prompt = (f"这是受控故障恢复题。先执行 {first}，该调用应失败。"
              f"收到错误后执行修正调用 {second}；不要重复错误。"
              + ("返回修正读取的全部文本。" if second["name"] == "read_file" else "成功后答 done。"))
    return make_case("error_recovery", name, variant, prompt, fixture,
                     [{**first, "ok": False}, second], expected=expected, answer=answer, edges=[["c0", "c1"]])


def build_tool_cases():
    builders = {"call_order": ordered, "parallel_calls": parallel, "error_recovery": recovery}
    rows = []
    for dimension in TOOL_DIMENSIONS:
        for family in range(6):
            for variant in range(5):
                if dimension in builders:
                    row = builders[dimension](family, variant)
                elif dimension in {"irrelevance", "missing_parameters"}:
                    row = no_call(dimension, family, variant)
                else:
                    row = simple(dimension, family, variant)
                rows.append(row)
    return rows
