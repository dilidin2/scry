"""Consensus analysis on comments (heuristic, pre-LLM).

Goal: for each comment give an idea of (a) whether people agree with it
and (b) how "reliable" it is as a signal.

The scoring is INTENTIONALLY simple and transparent: it is a filter that
prepares the ground for an LLM which then judges in context. It is NOT a
sentiment model: read the results as hints.

Logic:
- agreement_score: lexical heuristic (EN/IT agree/disagree markers)
- reliability: reliability proxy based on the comment's likes relative
  to the video's likes (>= 5% => "high"), with a replies-based bump for
  active threads; when the video's likes are unknown, falls back to
  >= 50% of the top comment's likes. A highly-liked comment is
  "validated" by the community (readers who agree upvote -> comment
  likes are the equivalent of a "yes" to that opinion)
"""
from __future__ import annotations

import re

# Agree markers (EN/IT) — case-insensitive
AGREE = [
    "grazie", "grazie mille", "verissimo", "assolutamente vero", "esatto",
    "fatto", "giusto", "perfetto", "corretto", "bravo", "brava", "vero ",
    "100%", "cento per cento", "proprio", "esattamente", "anche io", "stesso",
    "so true", "so real", "no cap", "facts", "fact", "exactly", "totally",
    "agreed", "yes", "right??", "so true", "deadass", "fr fr", "real",
    "preach", "undisputed", "relate", "so real", "100 percent",
    "\U0001F4AF", "\U0001F44D", "\U0001F525", "\U0001F62D", "\U0001F602\U0001F602", "\u2764\uFE0F", "\U0001F64F",
]

DISAGREE = [
    "falso", "sbagliato", "sbaglia", "non è vero", "non e vero", "non c'è",
    "ma va", "ma che", "niente a che", "non credo", "non ci credo", "dubito",
    "wrong", "false", "fake", "disagree", "not true", "nope", "nah",
    "youre wrong", "you're wrong", "that's not", "thats not", "lol no",
    "bruh", "\U0001F494", "\U0001F926",
]

# Emoji/neutral: don't count toward agreement, only toward length
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2700-\u27bf\u2b00-\u2bff]+"
)


def _sentiment(text: str) -> str:
    """'agree' | 'disagree' | 'neutral'."""
    t = " " + text.lower() + " "
    # disagreement first (more specific: "non è vero" contains "vero")
    if any(m in t for m in DISAGREE):
        return "disagree"
    if any(m in t for m in AGREE):
        return "agree"
    # very short text with only emoji/dots: neutral
    return "neutral"


def analyze_comments(comments: list[dict], video_likes: int | None = None) -> dict:
    """Analyze a list of comments (already sorted by likes, descending).

    Each comment: {username, text, likes, replies?}
    `video_likes`: total likes of the video/post itself (optional).

    Reliability thresholds:
    - "high": >= 5% of the video's likes (when `video_likes` is known),
      or an active thread (>= 20 replies) with >= 10 likes. Without a
      video like count to normalize on, the like-based criterion falls
      back to >= 50% of the top comment's likes.
    - "medium": >= 10 likes, below the "high" bar.
    - "low": fewer than 10 likes.

    Returns a dict with per-comment fields + aggregates.
    """
    if not comments:
        return {"available": False}

    likes_list = [c.get("likes") or 0 for c in comments]
    max_likes = max(likes_list) if likes_list else 0
    # "community validated" threshold: >= 5% of the video's likes when
    # known; otherwise >= 50% of the top comment's likes (relative).
    abs_floor = 10
    if video_likes:
        rel_floor = max(abs_floor, int(video_likes * 0.05))
    else:
        rel_floor = max(abs_floor, int(max_likes * 0.5))

    analyzed = []
    agree = disagree = neutral = 0
    for i, c in enumerate(comments):
        likes = c.get("likes") or 0
        replies = c.get("replies") or 0
        s = _sentiment(c.get("text", ""))
        agree += s == "agree"
        disagree += s == "disagree"
        neutral += s == "neutral"
        # reliability: high if community-validated (rel_floor) or an active thread
        if likes >= rel_floor or (replies >= 20 and likes >= abs_floor):
            reliability = "high"
        elif likes >= abs_floor:
            reliability = "medium"
        else:
            reliability = "low"
        analyzed.append({
            "rank": i + 1,
            "username": c.get("username"),
            "text": c.get("text", ""),
            "likes": likes,
            "replies": replies,
            "stance": s,
            "reliability": reliability,
            "likes_pct_of_video": round(100 * likes / video_likes, 3)
            if video_likes else None,
        })

    n = len(analyzed)
    return {
        "available": True,
        "total_analyzed": n,
        "agree": agree,
        "disagree": disagree,
        "neutral": neutral,
        "agreement_ratio": round(agree / max(1, agree + disagree), 2)
        if (agree + disagree) else None,
        "video_likes": video_likes,
        "comments": analyzed,
        "note": ("Stance/reliability are lexical heuristics (EN/IT) over likes: "
                 "comment likes = community validation. "
                 "Use them as a hint, not as truth."),
    }
