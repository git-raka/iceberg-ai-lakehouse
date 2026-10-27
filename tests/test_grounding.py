"""The agent's grounding check: every number in an answer must come from the tool results."""

from lakehouse.ai.agent import false_refusal, language, numbers, spelled_numbers, ungrounded
from lakehouse.ai.trino_client import Result

EVIDENCE = "SQL: ...\ntenant_id | net_tons\nT002 | 416.86\nT009 | 415.38\nfee | 118000000\nlease_end | 2035-10-31"


def test_numbers_understand_english_and_indonesian_formats():
    assert numbers("IDR 118,000,000")["118,000,000"] == 118_000_000
    assert numbers("Rp 118.000.000")["118.000.000"] == 118_000_000
    assert numbers("1.250,5 ton")["1.250,5"] == 1250.5
    assert numbers("416.86 tons")["416.86"] == 416.86
    assert numbers("3 tenants") == {}  # small counts are not checked


def test_answer_with_values_from_the_tools_is_grounded():
    assert ungrounded("T002 weighed 416.86 tons; the fee is Rp 118.000.000 until 31 October 2035.", EVIDENCE) == set()


def test_rounding_is_tolerated():
    assert ungrounded("T002 moved about 417 tons.", EVIDENCE) == set()


def test_invented_numbers_are_caught():
    assert ungrounded("Tenant T005 weighed a total of 50 tons this week.", EVIDENCE) == {"50"}
    assert ungrounded("The fee is IDR 250,000,000.", EVIDENCE) == {"250,000,000"}


def test_empty_or_all_null_results_tell_the_model_there_is_no_data():
    assert "no data visible" in Result(["x"], []).to_text()
    assert "no data visible" in Result(["sum"], [[None]]).to_text()


def test_refusing_although_rows_are_visible_is_caught():
    rows = [{"tool": "query_data", "rows": [["T001", "2027-12-31", 164000000]]}]
    assert false_refusal("I'm unable to retrieve the details due to permission restrictions.", rows)
    assert not false_refusal("T001's lease ends on 2027-12-31.", rows)


def test_refusing_masked_or_empty_results_is_correct():
    masked = [{"tool": "query_data", "rows": [["+62 ****73"]]}]
    assert not false_refusal("The phone number is restricted by data governance.", masked)
    assert not false_refusal("Maaf, data tersebut dibatasi.", [{"tool": "query_data", "rows": [[None]]}])
    assert not false_refusal("Unable to find it.", [{"tool": "search_documents", "hits": []}])


def test_reply_language_follows_the_question():
    assert language("Berapa gaji karyawan E0001?") == "Indonesian"
    assert language("Which leases expire next year?") == "English"


def test_numbers_spelled_out_in_documents_count_as_evidence():
    clause = "payable on the tenth day of each month; terminate with six months written notice"
    assert ungrounded("Biaya dibayar setiap tanggal 10.", clause) == set()
    assert spelled_numbers("the twenty-first day") == [21.0]
    assert ungrounded("Biaya dibayar setiap tanggal 15.", clause) == {"15"}
