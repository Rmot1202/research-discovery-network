from __future__ import annotations

import os

ALL_CS_CATEGORIES = [
    ("cs.AI", "Artificial Intelligence"),
    ("cs.AR", "Hardware Architecture"),
    ("cs.CC", "Computational Complexity"),
    ("cs.CE", "Computational Engineering, Finance, and Science"),
    ("cs.CG", "Computational Geometry"),
    ("cs.CL", "Computation and Language"),
    ("cs.CR", "Cryptography and Security"),
    ("cs.CV", "Computer Vision and Pattern Recognition"),
    ("cs.CY", "Computers and Society"),
    ("cs.DB", "Databases"),
    ("cs.DC", "Distributed, Parallel, and Cluster Computing"),
    ("cs.DL", "Digital Libraries"),
    ("cs.DM", "Discrete Mathematics"),
    ("cs.DS", "Data Structures and Algorithms"),
    ("cs.ET", "Emerging Technologies"),
    ("cs.FL", "Formal Languages and Automata Theory"),
    ("cs.GL", "General Literature"),
    ("cs.GR", "Graphics"),
    ("cs.GT", "Computer Science and Game Theory"),
    ("cs.HC", "Human-Computer Interaction"),
    ("cs.IR", "Information Retrieval"),
    ("cs.IT", "Information Theory"),
    ("cs.LG", "Machine Learning"),
    ("cs.LO", "Logic in Computer Science"),
    ("cs.MA", "Multiagent Systems"),
    ("cs.MM", "Multimedia"),
    ("cs.MS", "Mathematical Software"),
    ("cs.NA", "Numerical Analysis"),
    ("cs.NE", "Neural and Evolutionary Computing"),
    ("cs.NI", "Networking and Internet Architecture"),
    ("cs.OH", "Other Computer Science"),
    ("cs.OS", "Operating Systems"),
    ("cs.PF", "Performance"),
    ("cs.PL", "Programming Languages"),
    ("cs.RO", "Robotics"),
    ("cs.SC", "Symbolic Computation"),
    ("cs.SD", "Sound"),
    ("cs.SE", "Software Engineering"),
    ("cs.SI", "Social and Information Networks"),
    ("cs.SY", "Systems and Control"),
]

# Research scope: every arXiv CS category by default. To narrow it (for example to the URD
# MVP's AI/ML/NLP focus), set RESEARCH_SCOPE=mvp or a list such as RESEARCH_SCOPE="cs.AI,cs.LG".
MVP_SCOPE = (
    "cs.AI", "cs.LG", "cs.CL",            # core: AI, machine learning, NLP
    "cs.CV", "cs.IR", "cs.NE", "cs.MA",   # related ML areas
    "cs.RO", "cs.HC", "cs.CY",            # applied: robotics, human-AI interaction, AI & society
)
ALL_CODES = tuple(code for code, _ in ALL_CS_CATEGORIES)


def _scope_codes() -> tuple[str, ...]:
    configured = os.environ.get("RESEARCH_SCOPE", "").strip()
    if configured.lower() == "mvp":
        return MVP_SCOPE
    if configured and configured.lower() != "all":
        codes = tuple(code.strip() for code in configured.split(",") if code.strip() in ALL_CODES)
        if codes:
            return codes
    return ALL_CODES


SCOPE_CODES = _scope_codes()
CATEGORIES = [(code, label) for code, label in ALL_CS_CATEGORIES if code in SCOPE_CODES]
# Labels cover every CS category so papers cross-listed outside the scope still display nicely.
CATEGORY_LABELS = dict(ALL_CS_CATEGORIES)
CATEGORY_CODES = set(SCOPE_CODES)
