"""Sentiment signal, notifications, lessons dedup, and the new tools."""

from conftest import make_config

from stubs import StubBroker, make_account

from trading.data.journal import Journal
from trading.data.sentiment import get_symbol_sentiment
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


class FakeRedditPost:
    def __init__(self, title, score):
        self.title = title
        self.score = score


class FakeSubreddit:
    def __init__(self, posts):
        self._posts = posts

    def search(self, query, limit=10, time_filter="week"):
        return self._posts


class FakeReddit:
    def __init__(self, posts):
        self._posts = posts

    def subreddit(self, name):
        return FakeSubreddit(self._posts)


class NewsBroker(StubBroker):
    def __init__(self, account, news):
        super().__init__(account)
        self._news = news

    def get_news(self, symbol, limit=10):
        return self._news


def test_sentiment_polarity_from_news():
    news = [
        {"headline": "AAPL beats earnings, surges to record", "summary": "",
         "created_at": "2026-01-01", "source": "x"},
        {"headline": "AAPL upgrade drives strong rally", "summary": "",
         "created_at": "2026-01-01", "source": "x"},
    ]
    broker = NewsBroker(make_account(), news)
    sig = get_symbol_sentiment(make_config(), broker, "AAPL")
    assert sig.polarity > 0
    assert "positive" in sig.summary()


def test_sentiment_negative_and_reddit_mentions():
    news = [{"headline": "AAPL plunges on downgrade and weak guidance", "summary": "",
             "created_at": "2026-01-01", "source": "x"}]
    broker = NewsBroker(make_account(), news)
    reddit = FakeReddit([FakeRedditPost("$AAPL crash incoming, bearish", 50)])
    sig = get_symbol_sentiment(make_config(), broker, "AAPL", subreddits=("stocks",),
                               reddit=reddit)
    assert sig.polarity < 0
    assert sig.reddit_mentions == 1
    assert sig.reddit_score == 50


def test_sentiment_graceful_without_reddit():
    broker = NewsBroker(make_account(), [])
    sig = get_symbol_sentiment(make_config(), broker, "AAPL", reddit=None)
    assert sig.reddit_mentions == 0
    assert sig.polarity == 0.0


def test_notify_noop_without_webhook():
    from trading import notify

    config = make_config()  # no discord webhook in secrets
    assert notify.send(config, "hello") is False


def test_get_sentiment_tool(tmp_path):
    news = [{"headline": "AAPL surges", "summary": "", "created_at": "2026-01-01",
             "source": "x"}]
    broker = NewsBroker(make_account(), news)
    journal = Journal(tmp_path / "j.db")
    ctx = ToolContext(config=make_config(), journal=journal, broker=broker,
                      account_state=make_account(), agent_name="strategy")
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("get_sentiment", {"symbol": "AAPL"})
    assert "AAPL" in out and "polarity" in out


def test_read_playbook_tool(tmp_path):
    config = make_config()
    pb_dir = tmp_path / "playbooks"
    pb_dir.mkdir()
    (pb_dir / "test.md").write_text("# Playbook: test\nrules here", encoding="utf-8")
    config.settings.paths.playbooks_dir = str(pb_dir)
    journal = Journal(tmp_path / "j.db")
    ctx = ToolContext(config=config, journal=journal, broker=StubBroker(make_account()),
                      account_state=make_account())
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("read_playbook", {})
    assert "rules here" in out


def test_lessons_dedup(tmp_path):
    from trading.agents.scoring import append_lessons

    config = make_config()
    config.settings.paths.memory_dir = str(tmp_path / "memory")
    append_lessons(config, ["avoid earnings gaps", "size down on Fridays"])
    append_lessons(config, ["avoid earnings gaps", "new lesson"])  # first is a dup
    text = (tmp_path / "memory" / "lessons.md").read_text(encoding="utf-8")
    assert text.count("avoid earnings gaps") == 1
    assert "new lesson" in text
    assert "size down on Fridays" in text
