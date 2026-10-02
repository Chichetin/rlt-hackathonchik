"""robots.txt matcher: the rules of the real robots.txt of 2gis.ru (recorded 02.10.2026) and the Google
semantics we rely on — `*` and `$` in a path, the longest rule wins, Allow beats Disallow on a tie."""

from pathlib import Path

from src.sources.robots import allowed

REAL = (Path(__file__).resolve().parents[1] / "fixtures" / "gis2" / "robots.txt").read_text(encoding="utf-8")


def test_real_2gis_rules_pages_we_read_are_allowed():
    assert allowed(REAL, "/spb/search/%D0%A0%D0%BE%D1%81%D1%82%D0%B5%D0%BB%D0%B5%D0%BA%D0%BE%D0%BC")
    assert allowed(REAL, "/spb/firm/70000001104087830/tab/reviews")


def test_real_2gis_rules_wildcards_and_prefixes_are_disallowed():
    assert not allowed(REAL, "/_/anything")  # prefix with a wildcard tail: `/_/*`
    assert not allowed(REAL, "/spb/firm/1?printmode=1")  # `/*printmode=` — wildcard in the middle
    assert not allowed(REAL, "/spb/firm/1/my")  # `*/my$` — anchored at the end
    assert allowed(REAL, "/spb/firm/1/mystery")  # ...and `$` means exactly the end


def test_longest_rule_wins_and_allow_beats_disallow_on_tie():
    rules = "User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /c\nAllow: /c\n"
    assert not allowed(rules, "/a/x")
    assert allowed(rules, "/a/b/x")  # the longer Allow wins over the shorter Disallow
    assert allowed(rules, "/c")  # equal length: Allow wins


def test_group_selection_specific_agent_beats_star_and_empty_disallow_allows_all():
    rules = "User-agent: *\nDisallow: /\n\nUser-agent: RltBot\nDisallow:\n"
    assert not allowed(rules, "/x", agent="OtherBot")
    assert allowed(rules, "/x", agent="RltBot/1.0")


def test_no_rules_or_no_matching_group_means_allowed():
    assert allowed("", "/x")
    assert allowed("User-agent: GoogleBot\nDisallow: /\n", "/x", agent="RltBot")


def test_comments_blank_lines_and_case_of_directives_are_tolerated():
    rules = "# c\nuser-AGENT: *   # all\n\nDISALLOW: /p  # private\n"
    assert not allowed(rules, "/p/1")
    assert allowed(rules, "/q")
