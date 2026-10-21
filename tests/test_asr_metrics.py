from evals.asr_metrics import corpus_rates, edit_distance, normalize, score


def test_normalize_ignores_formatting_not_words() -> None:
    assert normalize("Встреча в 09:30, ёлка!") == ["встреча", "в", "9", "30", "елка"]
    assert normalize("в 9.30") == normalize("в 9 30")


def test_edit_distance() -> None:
    assert edit_distance("кот", "кит") == 1
    assert edit_distance(["a", "b", "c"], ["a", "c", "d"]) == 2
    assert edit_distance([], ["a"]) == 1


def test_score_counts_wrong_number_as_word_error_and_number_error() -> None:
    s = score("Напомни завтра в 9:30 позвонить Ивану", "напомни завтра в 9:40 позвонить ивану")
    assert s.word_errors == 1 and s.ref_words == 7
    assert not s.numbers_ok and not s.exact


def test_score_exact_despite_case_and_punctuation() -> None:
    s = score("Что у меня на сегодня", "что у меня на сегодня?")
    assert s.exact and s.numbers_ok and s.wer == 0


def test_corpus_rates_weight_by_length() -> None:
    rates = corpus_rates([score("a b c d", "a b c d"), score("x y", "")])
    assert rates["wer"] == 2 / 6
    assert rates["exact"] == 0.5
