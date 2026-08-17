"""Comment analysis: which comments the community has "validated".

Deliberately down to earth: no word matching, no sentiment. Stance
(agree/disagree) is left to whoever reads the comment — a human or an LLM,
which knows the difference between "no cap" and "no this video sucks".

What we compute is the one thing a number can say: whether a comment
gathered a meaningful share of the video's likes. Readers who agree
upvote, so comment likes are the community's "yes" — that's all this
module claims.

Per-comment reliability:
- "high": >= 5% of the video's likes (when `video_likes` is known), or an
  active thread (>= 20 replies) with >= 10 likes; when the video's likes
  are unknown, the like-based criterion falls back to >= 50% of the top
  comment's likes.
- "medium": >= 10 likes, below the "high" bar.
- "low": fewer than 10 likes.
"""
from __future__ import annotations


def analyze_comments(comments: list[dict], video_likes: int | None = None) -> dict:
    """Analyze a list of comments (already sorted by likes, descending).

    Each comment: {username, text, likes, replies?}
    `video_likes`: total likes of the video/post itself (optional).

    Returns a dict with per-comment reliability + aggregates.
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
    counts = {"high": 0, "medium": 0, "low": 0}
    for i, c in enumerate(comments):
        likes = c.get("likes") or 0
        replies = c.get("replies") or 0
        if likes >= rel_floor or (replies >= 20 and likes >= abs_floor):
            reliability = "high"
        elif likes >= abs_floor:
            reliability = "medium"
        else:
            reliability = "low"
        counts[reliability] += 1
        analyzed.append({
            "rank": i + 1,
            "username": c.get("username"),
            "text": c.get("text", ""),
            "likes": likes,
            "replies": replies,
            "reliability": reliability,
            "likes_pct_of_video": round(100 * likes / video_likes, 3)
            if video_likes else None,
        })

    n = len(analyzed)
    return {
        "available": True,
        "total_analyzed": n,
        "reliability": counts,
        "video_likes": video_likes,
        "comments": analyzed,
        "note": ("Reliability is a like-count heuristic: readers who agree "
                 "upvote, so a comment that gathers a meaningful share of "
                 "the video's likes is community-validated. Stance is up "
                 "to the reader of the comment itself."),
    }
