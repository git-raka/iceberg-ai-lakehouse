"""AI layer D: an agent that answers questions with tools over the governed lakehouse.

Tools: structured data (text-to-SQL through Trino), document search (pgvector, filtered by Trino),
and the list of datasets. The agent runs as the caller's identity end to end, so it can never see
more than the caller could see in a dashboard.
"""

from __future__ import annotations

import json
import re

from . import llm, text2sql, vectors

SYSTEM = """You are the data assistant of an industrial estate (a fictional simulation).
Answer only from tool results. Prefer query_data for numbers, lists and dates; use search_documents for what
contracts or certificates say. When a tool returns rows, they are data this user may see: answer with those
values. Only a value shown as *** or null is masked by data governance: say that value is restricted, never
guess it. If a tool returns no rows, say there is no matching data. Personal data of employees (national IDs,
individual salaries, contact details) is restricted by policy: say so plainly instead of calling tools for it.
Mention which table or document an answer comes from. Reply in the language of the question, briefly."""

# an answer that refuses although the tools returned visible values gets one retry
_REFUSAL = re.compile(r"(?i)\b(unable to|cannot (access|retrieve|provide)|can't|permission|access restriction|"
                      r"restricted|tidak dapat|tidak bisa|dibatasi|kebijakan privasi)")

TOOLS = [
    {"type": "function", "function": {
        "name": "query_data",
        "description": "Answer a question with SQL over the lakehouse tables (tenants, leases, weighbridge, "
                       "service tickets, procurement, headcount, e-bus fleet). Not for the wording of contracts "
                       "(payment day, notice period, utilities, handover): use search_documents for those. "
                       "Input: the user's question, passed on faithfully in its original language; do not "
                       "reinterpret it.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "search_documents",
        "description": "Semantic search over lease agreements and handover certificates (OCR'd text): what a "
                       "contract says about payment, termination, utilities, premises, or when a plot was handed "
                       "over. Input: a short English search query.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "list_datasets",
        "description": "List the available tables with a one-line description each.",
        "parameters": {"type": "object", "properties": {}}}},
]


def list_datasets() -> str:
    return "\n".join(f"{name}: {t['description']}" for name, t in text2sql.model()["tables"].items())


def run_tool(name: str, args: dict, user: str, original_question: str = "") -> tuple[str, dict]:
    """Execute one tool call; returns (text for the model, structured record for the caller)."""
    if name == "query_data":
        q = str(args.get("question", "")).strip()
        if not q or re.match(r"(?is)^\s*(select|with)\b", q):  # the model wrote SQL instead of a question
            q = original_question or q
            args["question"] = q
        r = text2sql.ask(q, user)
        if r.get("unanswerable"):
            return (f"The tables cannot answer this: {r['unanswerable']}. For what a contract or certificate "
                    "says, call search_documents; otherwise tell the user this data is not in the lakehouse."), r
        if r.get("error"):
            return f"Query failed: {r['error']}", r
        from .trino_client import Result
        return f"SQL: {r['sql']}\nResult ({len(r['rows'])} rows):\n{Result(r['columns'], r['rows']).to_text(30)}", r
    if name == "search_documents":
        hits = vectors.search(str(args.get("query", "")), user=user, k=4)
        text = "\n\n".join(f"[tenant {h['tenant_id']}, {h['file_name']} #{h['chunk_no']}, score {h['score']}]\n"
                           f"{h['text']}" for h in hits)
        if hits:  # say whose documents these are, so an answer cannot attribute them to another tenant
            text = (f"Documents this user may see belong to tenant(s): "
                    f"{', '.join(sorted({h['tenant_id'] for h in hits}))}.\n\n{text}")
        return text or "No matching documents you may access.", {"hits": hits}
    if name == "list_datasets":
        return list_datasets(), {}
    return f"Unknown tool {name}", {}


_NUMBER = re.compile(r"\d[\d.,]*\d|\d")


def numbers(text: str) -> dict[str, float]:
    """Numbers written in a text, parsed in English or Indonesian style: '118,000,000', '118.000.000',
    '1,250.5', '1.250,5', '416.86'. Values under 10 are ignored (counts, list markers, month numbers)."""
    out = {}
    for tok in _NUMBER.findall(text or ""):
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", tok):
            value = float(re.sub(r"[.,]", "", tok))
        elif re.fullmatch(r"\d{1,3}(,\d{3})+\.\d+", tok):
            value = float(tok.replace(",", ""))
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+", tok):
            value = float(tok.replace(".", "").replace(",", "."))
        else:
            try:
                value = float(tok.replace(",", ""))
            except ValueError:
                continue
        if value >= 10:
            out[tok] = value
    return out


_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fifth": 5, "eighth": 8, "ninth": 9, "twelfth": 12}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def spelled_numbers(text: str) -> list[float]:
    """Numbers written as English words in documents: 'tenth day', 'six months', 'twenty-first'."""
    words = {w: i for i, w in enumerate(_UNITS)}
    words |= {w + "th": i for w, i in words.items() if i >= 4} | _ORDINALS
    words |= {w.replace("y", "ieth"): v for w, v in _TENS.items()} | _TENS
    out = []
    compound = re.compile(r"(?i)\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-(\w+)")
    for tens, unit in compound.findall(text):
        if unit.lower() in words:
            out.append(_TENS[tens.lower()] + words[unit.lower()])
    out += [words[w.lower()] for w in re.findall(r"[A-Za-z]+", compound.sub(" ", text)) if w.lower() in words]
    return [float(v) for v in out]


def ungrounded(answer: str, evidence: str) -> set[str]:
    """Numbers in the answer that match no number in the question or tool results (1% rounding allowed)."""
    seen = list(numbers(evidence).values()) + spelled_numbers(evidence)
    return {tok for tok, v in numbers(answer).items()
            if not any(abs(v - e) <= max(0.01 * abs(e), 0.5) for e in seen)}


def visible_values(steps: list[dict]) -> bool:
    """True when a query returned at least one value that is neither NULL nor masked."""
    return any(v is not None and "***" not in str(v)
               for s in steps for row in s.get("rows") or [] for v in row)


def false_refusal(answer: str, steps: list[dict]) -> bool:
    return bool(_REFUSAL.search(answer or "")) and visible_values(steps)


def results_table(steps: list[dict]) -> str:
    from .trino_client import Result
    return "\n\n".join(f"SQL: {s['sql']}\n{Result(s['columns'], s['rows']).to_text(30)}"
                       for s in steps if s.get("rows"))


def answer_from_results(question: str, steps: list[dict], user: str) -> str:
    """Answer again from the query results alone, with no tools and no governance wording to over-apply.
    If that still refuses or invents numbers, return the results themselves."""
    table = results_table(steps)
    msg = llm.chat([{"role": "system", "content": "Write a short answer to the question using only the query "
                     "results given. The user is allowed to see these results. Values shown as *** or null are "
                     "masked: call them restricted. Mention the table. Reply in the language of the question."},
                    {"role": "user", "content": f"Question: {question}\n\n{table}"}],
                   purpose="agent-answer", user=user)
    answer = msg.content or ""
    if _REFUSAL.search(answer) or ungrounded(answer, question + "\n" + table):
        return f"Query results:\n\n{table}"
    return answer


_INDONESIAN = re.compile(r"(?i)\b(apa|apakah|berapa|siapa|kapan|mana|yang|dan|tidak|untuk|dengan|ini|itu)\b")


def language(question: str) -> str:
    """Small models drift into other languages; name the reply language explicitly."""
    return "Indonesian" if len(_INDONESIAN.findall(question)) >= 1 else "English"


def ask(question: str, user: str, max_steps: int = 5) -> dict:
    messages = [{"role": "system", "content": f"{SYSTEM}\nReply in {language(question)}."},
                {"role": "user", "content": question}]
    steps, evidence, retried, nudged = [], [question], False, False
    for _ in range(max_steps):
        msg = llm.chat(messages, purpose="agent", user=user, tools=TOOLS)
        if not msg.tool_calls and not steps and not nudged:  # an answer from memory is not an answer
            nudged = True
            messages += [{"role": "assistant", "content": msg.content or ""},
                         {"role": "user", "content": "You answered without calling a tool. Call search_documents "
                          "for what contracts or certificates say, or query_data for data, then answer from the "
                          "result. Only if the question asks for restricted employee personal data, repeat your "
                          "refusal without a tool."}]
            continue
        if not msg.tool_calls:
            bad = ungrounded(msg.content or "", "\n".join(evidence))
            if bad and not retried:  # one chance to answer again from the evidence only
                retried = True
                messages += [{"role": "assistant", "content": msg.content or ""},
                             {"role": "user", "content": f"Your answer contains numbers that are not in any tool "
                              f"result: {', '.join(sorted(bad))}. Answer again using only values from the tool "
                              "results; if there is no data, say so."}]
                continue
            answer = msg.content
            if false_refusal(answer or "", steps):  # small models over-apply the restriction rules
                answer = answer_from_results(question, steps, user)
                bad = ungrounded(answer, "\n".join(evidence))
            if bad:
                answer = ("I could not find data to answer this reliably (the values may not exist or may be "
                          "restricted for your role).")
            return {"question": question, "answer": answer, "steps": steps, "user": user,
                    "grounding": {"checked": True, "retried": retried, "ungrounded_numbers": sorted(bad)}}
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for call in msg.tool_calls:
            args = call.function.arguments if isinstance(call.function.arguments, dict) else \
                json.loads(call.function.arguments or "{}")
            text, record = run_tool(call.function.name, args, user, question)
            evidence.append(text)
            steps.append({"tool": call.function.name, "args": args, **{k: v for k, v in record.items()
                                                                         if k in ("sql", "rows", "columns", "error", "hits")}})
            messages.append({"role": "tool", "content": text[:6000], "tool_name": call.function.name})
    return {"question": question, "answer": "I could not finish within the step limit.", "steps": steps, "user": user}
