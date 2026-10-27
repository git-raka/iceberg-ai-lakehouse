"""AI layer D: an agent that answers questions with tools over the governed lakehouse.

Tools: structured data (text-to-SQL through Trino), document search (pgvector, filtered by Trino),
and the list of datasets. The agent runs as the caller's identity end to end, so it can never see
more than the caller could see in a dashboard.
"""

from __future__ import annotations

import json
import re

from . import llm, text2sql, vectors

SYSTEM = """You are the data assistant of an industrial estate (a fictional simulation). You can only read
data; you cannot change, create or delete anything.
Answer only from tool results. Prefer query_data for numbers, lists and dates; use search_documents for what
contracts or certificates say. When a tool returns rows, they are data this user may see: answer with those
values. Only a value shown as *** or null is masked by data governance: say that value is restricted, never
guess it. If a tool returns no rows, say there is no matching data. If a document does not state something
(for example a lease without an end date), say it is not stated; never fill it in. Personal data of employees
(national IDs, individual salaries, contact details) is restricted by policy: say so plainly instead of calling
tools for it. If the question is not about the estate's data, say briefly that you only answer questions about
tenants, leases, weighbridge tonnage, service tickets, procurement, headcount, the e-bus fleet and estate
documents. Mention which table or document an answer comes from. Reply in the language of the question,
briefly."""

# Deterministic refusals, decided before the model sees the question
_WRITE_REQUEST = re.compile(r"(?i)\b(drop|truncate|alter|grant|revoke|insert\s+into|delete\s+from|update\s+[\w.]+\s+set|"
                            r"create\s+(table|view|schema)|merge\s+into)\b")
_RAW_SQL = re.compile(r"(?is)\bselect\b.+\bfrom\s+[\w\"]+\.|\bsystem\.\w+|\buse\s+catalog\b")
READ_ONLY = ("I can only read data; I cannot change, create or delete anything, and nothing was changed. "
             "Ask a question about the estate's data instead.")
RAW_SQL = ("I answer questions in plain language and do not run SQL written in the question. To run an exact "
           "read-only query, use POST /v1/sql (checked by the SQL guard and the same access rules).")
READ_ONLY_ID = ("Saya hanya bisa membaca data; saya tidak bisa mengubah, membuat, atau menghapus apa pun, dan tidak "
                "ada yang diubah. Silakan ajukan pertanyaan tentang data estate.")
RAW_SQL_ID = ("Saya menjawab pertanyaan dalam bahasa biasa dan tidak menjalankan SQL yang ditulis di pertanyaan. "
              "Untuk query read-only yang persis, gunakan POST /v1/sql (diperiksa SQL guard dan aturan akses yang sama).")

# questions that clearly need data or documents: if the model answers them without a tool, run the tool for it
_ON_TOPIC = re.compile(r"(?i)\b(tenants?|lease|leases|sewa|kontrak|contract|plot|lahan|zona|zone|bus|buses|baterai|"
                       r"battery|penumpang|passengers|tickets?|tiket|tons?|tonase|ditimbang|weighbridge|purchase|"
                       r"procurement|pengadaan|po|headcount|karyawan|employees?|departemen|department|assets?|aset|"
                       r"fee|biaya|handover|serah\s*terima|certificate|sertifikat|documents?|dokumen|sla|T\d{3})\b")
_DOCUMENT = re.compile(r"(?i)\b(say|says|said|clause|klausul|isi|bunyi|menurut|according|document|dokumen|"
                       r"termination|terminate|pemutusan|mengakhiri|notice|pemberitahuan|utilities|utilitas|payment day|"
                       r"payable|tanggal bayar|dibayar|handover|serah\s*terima|certificate|sertifikat|signed|ditandatangani)\b")
# a date written in an answer must appear (as the same date) in the evidence
_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                            "september", "october", "november", "december"])}
_MONTHS |= {m: i + 1 for i, m in enumerate(["januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus",
                                             "september", "oktober", "november", "desember"])}
_MONTHS |= {m[:3]: i for m, i in list(_MONTHS.items())}

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
            return f"The tables do not hold this: {r['unanswerable']}", r
        if r.get("error"):
            return f"Query failed: {r['error']}", r
        from .trino_client import Result
        scope = (f"Rows are limited to what {user} may see (its own tenant only): do not present them as a "
                 "ranking or comparison of all tenants.\n" if user.startswith("tenant_") else "")
        return (f"{scope}SQL: {r['sql']}\nResult ({len(r['rows'])} rows):\n"
                f"{Result(r['columns'], r['rows']).to_text(30)}"), r
    if name == "search_documents":
        query_text = str(args.get("query", ""))
        # a question about named tenants is answered from their documents only
        asked = set(re.findall(r"\bT\d{3}\b", f"{original_question} {query_text}".upper()))
        hits = vectors.search(query_text, user=user, k=4, tenants=sorted(asked) or None)
        text = "\n\n".join(f"[tenant {h['tenant_id']}, {h['file_name']} #{h['chunk_no']}, score {h['score']}]\n"
                           f"{h['text']}" for h in hits)
        owners = {h["tenant_id"] for h in hits}
        if hits:  # say whose documents these are, so an answer cannot attribute them to another tenant
            text = f"Documents this user may see belong to tenant(s): {', '.join(sorted(owners))}.\n\n{text}"
        if asked - owners:
            text = (f"No documents of {', '.join(sorted(asked - owners))} were found for this user: say so, and do "
                    f"not describe another tenant's documents as theirs.\n\n{text}")
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


def dates(text: str) -> set[str]:
    """Dates in a text as ISO strings: '2035-10-31', '31 October 2035', '31 Oktober 2035', 'October 31, 2035',
    '31/10/2035'. Checking whole dates matters: day and year can each appear somewhere else in the evidence."""
    out = {f"{y}-{m}-{d}" for y, m, d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)", text)}
    for d, mon, y in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})\b", text):
        if mon.lower() in _MONTHS:
            out.add(f"{y}-{_MONTHS[mon.lower()]:02d}-{int(d):02d}")
    for mon, d, y in re.findall(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        if mon.lower() in _MONTHS:
            out.add(f"{y}-{_MONTHS[mon.lower()]:02d}-{int(d):02d}")
    for d, m, y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text):
        out.add(f"{y}-{int(m):02d}-{int(d):02d}")
    return out


def ungrounded(answer: str, evidence: str) -> set[str]:
    """Numbers and dates in the answer that appear nowhere in the question or tool results
    (numbers may differ by 1% rounding; dates must match exactly)."""
    seen = list(numbers(evidence).values()) + spelled_numbers(evidence)
    bad = {tok for tok, v in numbers(answer).items()
           if not any(abs(v - e) <= max(0.01 * abs(e), 0.5) for e in seen)}
    return bad | (dates(answer) - dates(evidence))


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


_INDONESIAN = re.compile(r"(?i)\b(apa|apakah|berapa|siapa|kapan|mana|yang|dan|tidak|untuk|dengan|ini|itu|tolong|"
                         r"tampilkan|berikan|sebutkan|bagaimana|kenapa|mengapa|jumlah|semua|setiap|sudah|belum|"
                         r"adalah|dari|pada|ke|di|saya|kami|bulan|minggu|tahun|hari)\b")


def language(question: str) -> str:
    """Small models drift into other languages; name the reply language explicitly."""
    return "Indonesian" if len(_INDONESIAN.findall(question)) >= 1 else "English"


def ask(question: str, user: str, max_steps: int = 6) -> dict:
    indonesian = language(question) == "Indonesian"
    for pattern, refusal, kind in ((_WRITE_REQUEST, READ_ONLY_ID if indonesian else READ_ONLY, "write"),
                                   (_RAW_SQL, RAW_SQL_ID if indonesian else RAW_SQL, "raw_sql")):
        if pattern.search(question):
            return {"question": question, "answer": refusal, "steps": [], "user": user,
                    "grounding": {"checked": True, "refused": kind}}
    messages = [{"role": "system", "content": f"{SYSTEM}\nReply in {language(question)}."},
                {"role": "user", "content": question}]
    steps, evidence, retried, forced = [], [question], False, set()

    def call(name: str, args: dict) -> None:
        text, record = run_tool(name, args, user, question)
        evidence.append(text)
        steps.append({"tool": name, "args": args, **{k: v for k, v in record.items()
                                                     if k in ("sql", "rows", "columns", "error", "hits")}})
        messages.append({"role": "tool", "content": text[:6000], "tool_name": name})

    def force(name: str, args: dict) -> None:
        """Run a tool the model should have called, as if it had."""
        forced.add(name)
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": name, "arguments": args}}]})
        call(name, args)

    for _ in range(max_steps):
        msg = llm.chat(messages, purpose="agent", user=user, tools=TOOLS)
        used = {s["tool"] for s in steps}
        if not msg.tool_calls and _ON_TOPIC.search(question):
            # an on-topic answer without the tool that holds the facts is a guess: fetch them and ask again
            if _DOCUMENT.search(question) and "search_documents" not in used | forced:
                force("search_documents", {"query": question})
                continue
            if not used and "query_data" not in forced:
                force("query_data", {"question": question})
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
                    "grounding": {"checked": True, "retried": retried, "ungrounded": sorted(bad),
                                  "forced_tools": sorted(forced)}}
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for c in msg.tool_calls:
            args = c.function.arguments if isinstance(c.function.arguments, dict) else \
                json.loads(c.function.arguments or "{}")
            name = c.function.name
            if name == "query_data" and _WRITE_REQUEST.search(str(args.get("question", ""))):
                args["question"] = question  # never forward a rewritten write request
            call(name, args)
    return {"question": question, "answer": "I could not finish within the step limit.", "steps": steps, "user": user}
