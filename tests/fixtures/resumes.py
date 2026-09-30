"""A fictional resume, built on the fly in the formats Resumap accepts.

Generated rather than committed so no real person's resume ever lands in the
repository, and so the source of every fixture is readable in review. The
content is laid out the way real resumes are: a centred name and contact line,
ruled section headings, entries with a right-aligned date, and bullets long
enough to wrap onto a second line.
"""

from __future__ import annotations

from pathlib import Path

NAME = "Jane Doe"
CONTACT = "jane.doe@example.com | (555) 010-0199 | linkedin.com/in/janedoe | Springfield, USA"

# (section, [(entry left, entry right, [bullets])]) in page order.
SECTIONS: list[tuple[str, list[tuple[str, str, list[str]]]]] = [
    (
        "EXPERIENCE",
        [
            (
                "Senior Data Engineer, Northwind Analytics",
                "Jan 2021 - Present",
                [
                    "Built a streaming ingestion pipeline in Python and Kafka that processed "
                    "2 million events per day at 99.9% uptime across three regions.",
                    "Reduced nightly batch runtime by 40% by rewriting Spark jobs and partitioning "
                    "Parquet tables by date.",
                    "Led a team of 4 engineers to migrate 120 Airflow DAGs from a legacy cluster "
                    "to managed cloud infrastructure without missing a delivery window.",
                ],
            ),
            (
                "Data Engineer, Contoso Group",
                "Mar 2018 - Dec 2020",
                [
                    "Designed a dimensional data model in PostgreSQL that cut report query times "
                    "from 30 seconds to under 3.",
                    "Automated data quality checks with Great Expectations, catching 95% of schema "
                    "drift before it reached dashboards.",
                ],
            ),
        ],
    ),
    (
        "PROJECTS",
        [
            (
                "Open Transit Dashboard",
                "2022",
                [
                    "Developed a public dashboard in React and FastAPI that shows real-time bus "
                    "arrivals to 30,000 monthly users.",
                ],
            ),
        ],
    ),
    (
        "EDUCATION",
        [
            ("Example University, B.S. Computer Science", "2014 - 2018", []),
        ],
    ),
]
SKILLS = [
    "Languages: Python, SQL, Scala, TypeScript",
    "Tools: Kafka, Spark, Airflow, dbt, PostgreSQL, Docker",
]

BULLETS = [b for _, entries in SECTIONS for _, _, bs in entries for b in bs]


def build_pdf(path: Path) -> Path:
    """Draw the resume with ReportLab, one letter-size page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    width, height = letter
    left, right = 54.0, width - 54.0
    bullet_x, text_x = 62.0, 72.0
    size, leading = 10.0, 12.5

    c = canvas.Canvas(str(path), pagesize=letter)
    y = height - 60.0
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, y, NAME)
    y -= 18
    c.setFont("Helvetica", 9.5)
    c.drawCentredString(width / 2, y, CONTACT)
    y -= 22

    def section(title: str) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 11)
        c.drawString(left, y, title)
        c.setLineWidth(0.6)
        c.line(left, y - 3, right, y - 3)
        y -= 16

    for title, entries in SECTIONS:
        section(title)
        for head, when, bullets in entries:
            c.setFont("Helvetica-Bold", size)
            c.drawString(left, y, head)
            c.setFont("Helvetica", size)
            c.drawRightString(right, y, when)
            y -= leading
            for text in bullets:
                lines = simpleSplit(text, "Helvetica", size, right - text_x)
                c.drawString(bullet_x, y, "•")
                for line in lines:
                    c.drawString(text_x, y, line)
                    y -= leading
            y -= 6
        y -= 4

    section("SKILLS")
    c.setFont("Helvetica", size)
    for line in SKILLS:
        c.drawString(left, y, line)
        y -= leading
    c.showPage()
    c.save()
    return path


def build_docx(path: Path) -> Path:
    """The same content as a Word document using built-in list styles."""
    import docx

    d = docx.Document()
    d.add_paragraph(NAME, style="Title")
    d.add_paragraph(CONTACT)
    for title, entries in SECTIONS:
        d.add_paragraph(title, style="Heading 2")
        for head, when, bullets in entries:
            p = d.add_paragraph()
            p.add_run(f"{head}\t{when}").bold = True
            for text in bullets:
                d.add_paragraph(text, style="List Bullet")
    d.add_paragraph("SKILLS", style="Heading 2")
    for line in SKILLS:
        d.add_paragraph(line)
    d.save(str(path))
    return path


def build_txt(path: Path) -> Path:
    """Plain text: capitalised headings, dash bullets."""
    out = [NAME, CONTACT, ""]
    for title, entries in SECTIONS:
        out += [title]
        for head, when, bullets in entries:
            out += [f"{head}    {when}"] + [f"- {b}" for b in bullets]
        out += [""]
    out += ["SKILLS", *SKILLS]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


# A posting with the shapes split_points has to cope with: blurb before the
# first heading, bullets, a hard-wrapped requirement, and trailing boilerplate.
JOB_DESCRIPTION = """Contoso is hiring a Data Engineer to join our platform team.
We move fast and care about reliable data.

Responsibilities:
- Build and operate streaming pipelines with Kafka and Python.
- Own batch processing on Spark and keep nightly jobs within their SLA.
- Partner with analysts to design data models in PostgreSQL that make
  reporting fast and self-serve.

Requirements:
- 3+ years of experience with Airflow or a similar orchestrator.
- Experience building dashboards with React.

Benefits
- Unlimited PTO and a home office stipend.

Contoso is an equal opportunity employer.
"""

# What split_points must return for JOB_DESCRIPTION, character for character.
JOB_POINTS = [
    "Build and operate streaming pipelines with Kafka and Python.",
    "Own batch processing on Spark and keep nightly jobs within their SLA.",
    "Partner with analysts to design data models in PostgreSQL that make "
    "reporting fast and self-serve.",
    "3+ years of experience with Airflow or a similar orchestrator.",
    "Experience building dashboards with React.",
]
