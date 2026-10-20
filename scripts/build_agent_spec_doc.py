from __future__ import annotations

from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


OUTPUT = Path("deliverables/Mica_Current_Agent_Setup_Spec.docx")

NAVY = "17324D"
BLUE = "2D6A9F"
PALE_BLUE = "EAF2F8"
PALE_GRAY = "F4F6F7"
MID_GRAY = "6B7280"
LIGHT_BORDER = "D9D9D9"
BLACK = "000000"
WHITE = "FFFFFF"


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top: int = 80, start: int = 120, bottom: int = 80, end: int = 120) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_border(cell, color: str = LIGHT_BORDER, size: str = "6") -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        border = borders.find(qn(tag))
        if border is None:
            border = OxmlElement(tag)
            borders.append(border)
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), size)
        border.set(qn("w:color"), color)


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:tblHeader")) is None:
        header = OxmlElement("w:tblHeader")
        header.set(qn("w:val"), "true")
        tr_pr.append(header)


def prevent_row_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is None:
        tr_pr.append(OxmlElement("w:cantSplit"))


def set_table_widths(table, widths: list[float]) -> None:
    for row in table.rows:
        for idx, width in enumerate(widths):
            row.cells[idx].width = Inches(width)


def add_table(doc: Document, headers: list[str], rows: list[list[str]], widths: list[float]):
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.style = "Table Grid"
    header = table.rows[0]
    set_repeat_table_header(header)
    prevent_row_split(header)
    for idx, label in enumerate(headers):
        cell = header.cells[idx]
        set_cell_shading(cell, NAVY)
        set_cell_margins(cell)
        set_cell_border(cell)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(label)
        run.bold = True
        run.font.color.rgb = RGBColor(255, 255, 255)
        run.font.size = Pt(9)
    for row_idx, values in enumerate(rows):
        row = table.add_row()
        prevent_row_split(row)
        for col_idx, value in enumerate(values):
            cell = row.cells[col_idx]
            set_cell_margins(cell)
            set_cell_border(cell)
            if row_idx % 2:
                set_cell_shading(cell, PALE_BLUE)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.05
            run = p.add_run(value)
        run.font.size = Pt(8.4)
    set_table_widths(table, widths)
    after = doc.add_paragraph()
    after.paragraph_format.space_after = Pt(2)
    return table


def add_bullet(doc: Document, text: str, level: int = 0) -> None:
    p = doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
    p.paragraph_format.space_after = Pt(2)
    p.add_run(text)


def add_number(doc: Document, number: int, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.28)
    p.paragraph_format.first_line_indent = Inches(-0.28)
    p.paragraph_format.space_after = Pt(3)
    lead = p.add_run(f"{number}.  ")
    lead.bold = True
    p.add_run(text)


def add_source(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(text)
    run.font.name = "Aptos"
    run.font.size = Pt(8.3)
    run.font.color.rgb = RGBColor(75, 85, 99)


def keep_with_next(paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.append(OxmlElement("w:keepNext"))


def add_heading(doc: Document, text: str, level: int = 1) -> None:
    p = doc.add_heading(text, level=level)
    keep_with_next(p)


def configure_styles(doc: Document) -> None:
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Aptos"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor(17, 24, 39)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.12

    title = styles["Title"]
    title.font.name = "Aptos Display"
    title.font.size = Pt(27)
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title.paragraph_format.space_after = Pt(14)
    title_ppr = title._element.get_or_add_pPr()
    title_border = title_ppr.find(qn("w:pBdr"))
    if title_border is not None:
        title_ppr.remove(title_border)

    subtitle = styles["Subtitle"]
    subtitle.font.name = "Aptos"
    subtitle.font.size = Pt(13)
    subtitle.font.color.rgb = RGBColor(55, 65, 81)
    subtitle.paragraph_format.space_after = Pt(18)

    for name, size, before, after in (
        ("Heading 1", 18, 18, 8),
        ("Heading 2", 13, 13, 6),
        ("Heading 3", 11, 10, 4),
    ):
        style = styles[name]
        style.font.name = "Aptos Display"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    for name in ("List Bullet", "List Bullet 2", "List Number"):
        style = styles[name]
        style.font.name = "Aptos"
        style.font.size = Pt(10.5)


def build() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    configure_styles(doc)
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.72)
    section.bottom_margin = Inches(0.55)
    section.left_margin = Inches(0.78)
    section.right_margin = Inches(0.78)

    title = doc.add_paragraph(style="Title")
    title.add_run("Mica Current Agent Setup Specification")
    subtitle = doc.add_paragraph(style="Subtitle")
    subtitle.add_run("Runtime status local development recommendation and prompt architecture review")
    meta = doc.add_paragraph()
    meta.paragraph_format.space_after = Pt(20)
    run = meta.add_run("Prepared for repository review  |  28 September 2026  |  Version 1")
    run.font.size = Pt(9.5)
    run.font.color.rgb = RGBColor(75, 85, 99)

    add_heading(doc, "Executive conclusion", 1)
    p = doc.add_paragraph()
    p.add_run("Mica is not running on this MacBook right now. ").bold = True
    p.add_run(
        "The source tree and local Python environment are present, but no service responds on port "
        "8080, no local .env file exists, and the Docker daemon is not running. A Google Cloud "
        "staging deployment does exist and is reachable; its root endpoint redirects to the "
        "Mica login flow."
    )
    p = doc.add_paragraph()
    p.add_run("Recommendation ").bold = True
    p.add_run(
        "Use a local first development setup for the API and isolated science worker, while keeping "
        "the existing cloud staging environment for end to end integration. Do not reproduce the "
        "entire Google Cloud stack on the Mac. The production design is not one permanent VM; Cloud "
        "Run hosts the control plane and Google Cloud Batch creates temporary worker VMs only when "
        "an approved analysis runs."
    )

    add_heading(doc, "Current runtime snapshot", 1)
    add_table(
        doc,
        ["Question", "Observed state", "Basis"],
        [
            [
                "Is Mica running locally",
                "No",
                "HTTP checks to localhost port 8080 failed and no listener was found",
            ],
            [
                "Is the local container stack running",
                "No",
                "Docker reported that its daemon is not running",
            ],
            [
                "Is local runtime configuration installed",
                "Not yet",
                "The repository has .env.example but no .env file",
            ],
            [
                "Is a cloud deployment represented",
                "Yes",
                "Saved Terraform state contains 59 managed resource instances and a Cloud Run URL",
            ],
            [
                "Is the saved staging endpoint reachable",
                "Yes",
                "The Cloud Run endpoint returned HTTP 307 to the login route on 28 September 2026",
            ],
        ],
        [1.55, 1.15, 4.2],
    )
    p = doc.add_paragraph()
    p.add_run("Confidence note ").bold = True
    p.add_run(
        "The local checks describe this Mac at review time. The cloud resource inventory comes from "
        "the repository's saved Terraform state, while endpoint reachability was checked directly. "
        "The review did not query the Google Cloud administrative APIs for drift."
    )

    add_heading(doc, "Current deployment architecture", 1)
    p = doc.add_paragraph(
        "The codebase has two runtime profiles. Local development keeps orchestration inside one API "
        "process and executes generated analysis code either directly or in a restricted Docker "
        "container. Staging and production use managed Google Cloud services and submit each analysis "
        "as an immutable Batch job."
    )
    add_table(
        doc,
        ["Concern", "Local profile", "Cloud profile"],
        [
            ["Control plane", "FastAPI on the Mac or in Docker", "Cloud Run service"],
            ["Task dispatch", "In process asynchronous task", "Cloud Tasks queue with concurrency capped at three"],
            ["State", "In memory repository", "Firestore"],
            ["Analysis execution", "Local process or no network Docker container", "Ephemeral Google Cloud Batch VM"],
            ["Artifacts", "Local .mica directory", "Cloud Storage task prefixes"],
            ["Images", "Locally built API and science images", "Artifact Registry images pinned by digest"],
            ["Events", "Direct service calls", "Pub Sub for completion Gmail and tool build events"],
            ["Scheduled maintenance", "None", "Cloud Scheduler for compaction reconciliation Gmail watch and catalog work"],
            ["Secrets", "Environment file or shell environment", "Secret Manager references"],
            ["Authentication", "Development identity", "OIDC bearer tokens and application access controls"],
        ],
        [1.4, 2.6, 2.9],
    )
    p = doc.add_paragraph()
    p.add_run("Cloud control plane sizing ").bold = True
    p.add_run(
        "The Terraform definition gives Cloud Run 1 CPU and 1 GiB of memory, a 900 second request "
        "timeout, zero minimum instances, and three maximum instances. Analysis workers use one of "
        "four fixed profiles from 2 CPU and 8 GiB through an L4 GPU profile. Workers have no external "
        "IP address."
    )

    add_heading(doc, "Recommended local setup", 1)
    p = doc.add_paragraph(
        "Install only the components needed to develop and validate the agent loop. Keep managed cloud "
        "services in staging, where Slack Gmail OIDC Cloud Tasks Pub Sub and Batch behavior can be "
        "tested against their real interfaces."
    )
    add_number(doc, 1, "Install and start Docker Desktop. On macOS its Linux VM supplies the container runtime Mica needs.")
    add_number(doc, 2, "Build the science image named mica-science:local and start the API with Docker Compose.")
    add_number(doc, 3, "Create a local .env from .env.example and add only the development OpenAI key and nonproduction settings.")
    add_number(doc, 4, "Use the headless API and local fixture for the normal edit test loop; interact with the agent in Slack.")
    add_number(doc, 5, "Use cloud staging for channel callbacks managed identity Batch networking and recovery tests.")

    add_heading(doc, "Local limitations to accept or fix", 2)
    add_bullet(doc, "The in memory repository loses projects tasks approvals and memories whenever the API restarts.")
    add_bullet(doc, "The current Compose profile mounts the Docker socket into the API container so the API can launch isolated worker containers.")
    add_bullet(doc, "Slack and Gmail callbacks need a public endpoint or staging deployment and are not naturally local only workflows.")
    add_bullet(doc, "Local Docker on Apple Silicon can differ from the linux amd64 images expected by the approved tool registry and cloud workers.")
    add_bullet(doc, "The offline planner used without an OpenAI key is deterministic test behavior and is not a scientific substitute.")
    p = doc.add_paragraph()
    p.add_run("Near term improvement ").bold = True
    p.add_run(
        "Add a durable local state option before relying on the Mac for multi day work. A documented "
        "Firestore emulator profile or a small SQLite repository would close the largest gap without "
        "requiring the rest of Google Cloud to run locally."
    )

    add_heading(doc, "Current agent and prompt architecture", 1)
    p = doc.add_paragraph(
        "Planner version 2 is the default for newly created tasks. It uses one authoritative master "
        "plan, deterministic approval and resource stages, bounded model workers for figures and "
        "code, and a separate public message writer. Planner version 1 remains in the repository so "
        "older tasks can finish under the workflow that created them."
    )

    add_heading(doc, "Prompt execution sequence", 2)
    add_number(doc, 1, "The intake Conversation Agent converts the raw user message into StructuredUserRequest. It may not add scientific choices or requested outputs.")
    add_number(doc, 2, "The Context Builder creates the model input from safety policy current request structured request provisional contract authorized project metadata recent history memory dataset metadata and tool registry records.")
    add_number(doc, 3, "The Master Planner returns one typed MasterPlan with the task contract steps primary tools parameters figure intents and a short response.")
    add_number(doc, 4, "Policy code asks for approval only for scientific and interpretation changing visual parameters. Approved values are stored in a DecisionSet tied to the plan ID and revision.")
    add_number(doc, 5, "Normal tool matching and compute sizing are deterministic. The Tool Manager model with web search runs only when the registry cannot resolve a requested tool.")
    add_number(doc, 6, "The Figure Planner chooses presentation settings without changing approved figure semantics.")
    add_number(doc, 7, "The Code Builder produces a typed CodeBuildResult and a reproducible Python program. Static validation and a real data preflight gate execution.")
    add_number(doc, 8, "The public Conversation Agent sees a reduced safe task view and writes selected approval result and failure messages. A deterministic presenter is the fallback.")

    add_heading(doc, "Agent inventory", 2)
    add_table(
        doc,
        ["Agent", "When used", "Model and bounds", "Typed result and authority"],
        [
            [
                "Conversation Agent for intake",
                "Every task when an API key is present",
                "OpenAI model setting currently gpt-5.6-sol medium reasoning maximum three turns",
                "StructuredUserRequest. Parses only and cannot choose science tools or answers",
            ],
            [
                "Master Planner",
                "Planner version 2 once per plan revision",
                "gpt-5.6-sol medium reasoning maximum two turns 180 second guardrail",
                "MasterPlan. Defines contract procedures parameters dependencies and figure intents",
            ],
            [
                "Tool Manager",
                "Planner version 2 only for unknown tools and planner version 1 as a subtool",
                "gpt-5.6-sol medium reasoning web search maximum two turns in version 2",
                "ToolPlan. May propose an exact tool version but cannot install or execute it",
            ],
            [
                "Figure Planner",
                "When the master plan includes figure intents",
                "gpt-6-luna low reasoning maximum two turns 150 second guardrail with gpt-5.6-sol fallback",
                "FigurePlan. Controls presentation only and must preserve approved semantics",
            ],
            [
                "Code Builder",
                "Executable planner version 2 tasks",
                "gpt-6-luna low reasoning maximum two turns per call 150 second guardrail with fallback and repair calls",
                "CodeBuildResult. Writes minimal approved code and declares runtime tool IDs",
            ],
            [
                "Conversation Agent for output",
                "Approval input success and failure messages when an API key is present",
                "gpt-5.6-sol low reasoning maximum two turns",
                "UserFacingMessage. Receives only a sanitized public view and cannot change state",
            ],
            [
                "Query Agent for legacy tasks",
                "Planner version 1 only",
                "gpt-5.6-sol medium reasoning maximum twelve turns with resource and tool subagents",
                "AgentTaskPlan. Combines contract resources tools code response and memory candidates",
            ],
            [
                "Resource Orchestrator for legacy tasks",
                "Called as a Query Agent tool",
                "gpt-5.6-sol medium reasoning maximum four tool turns",
                "ResourcePlan. Recommends one fixed profile and cannot provision it",
            ],
        ],
        [1.25, 1.65, 2.05, 2.0],
    )

    add_heading(doc, "Shared model behavior", 2)
    add_bullet(doc, "All model calls set store to false and default Agents SDK tracing is disabled.")
    add_bullet(doc, "Responses use low verbosity automatic truncation and Pydantic structured output schemas.")
    add_bullet(doc, "Master Planner Figure Planner and Code Builder disable parallel tool calls.")
    add_bullet(doc, "The legacy general agent settings allow parallel tool calls because the Query Agent can call resource and tool specialists.")
    add_bullet(doc, "Planner stages record model reasoning token use latency attempts hashes and status so a failed downstream stage can resume without rerunning a completed master plan.")

    add_heading(doc, "Context assembly", 1)
    p = doc.add_paragraph(
        "The Context Builder is the effective user prompt composer for the planning agents. It checks "
        "project access before assembly and places labeled blocks into one rendered string. The safety "
        "policy treats repositories attachments tool output and memories as untrusted data."
    )
    add_table(
        doc,
        ["Block", "Current contents", "Trust and retention"],
        [
            ["Policy", "Scientific authorization and prompt injection rules", "Highest instruction block in the rendered context"],
            ["Current request", "Lossless raw user message", "Current task evidence"],
            ["Structured request", "Objective outputs constraints explicit settings questions", "Model parsed handoff plus raw message retained"],
            ["Task contract", "Objective inputs authorized actions missing decisions prohibited extensions", "Current task scope"],
            ["Project", "Project ID slug name members and task status", "Authorized metadata but includes internal identifiers"],
            ["Recent exchanges", "At most two prior task requests and result summary or status", "Project scoped advisory history"],
            ["Project summary", "Compacted completed task summary", "Advisory"],
            ["Retrieved memory", "Up to eight active or pinned items", "Advisory and explicitly untrusted"],
            ["Input metadata", "Attachment references checksums and generations", "Verified metadata"],
            ["Dataset schema", "Observed dimensions fields categories and suggested keys", "Verified metadata with unapproved suggestions"],
            ["Tool metadata", "Available pinned tools and unavailable catalog entries", "Available entries are approved registry records"],
        ],
        [1.25, 3.2, 2.5],
    )
    p = doc.add_paragraph()
    p.add_run("Budgets and compaction ").bold = True
    p.add_run(
        "Retrieved memory has an 8000 estimated token budget and a limit of eight items. The complete "
        "rendered context has a 40000 estimated token ceiling. Lowest ranked memories are removed "
        "first, followed by older exchanges. Project compaction is scheduled after five completed "
        "tasks or about 60000 uncompacted input tokens."
    )

    add_heading(doc, "Prompt responsibilities and constraints", 1)
    add_table(
        doc,
        ["Prompt", "Primary instruction", "Important prohibitions"],
        [
            ["Intake", "Parse the exact request into a typed handoff", "No added science tools answers or deliverables"],
            ["Safety policy", "Limit action to the current TaskContract", "Memory cannot authorize work or override the harness"],
            ["Master planning", "Describe primary procedures and classify parameters", "No code web inspection compute choice or library argument inventory"],
            ["Tool selection", "Use exact versions and review output affecting parameters", "No installation execution repository writes or unapproved tools"],
            ["Figure planning", "Choose presentation settings and QA checks", "No change to normalization aggregation filtering scales or semantics"],
            ["Code building", "Implement the immutable plan with real data preflight", "No network credentials cloud APIs Git tuning extra analyses or artifacts"],
            ["Public output", "Condense a safe task view into one message", "No new facts claims state changes IDs paths or infrastructure"],
        ],
        [1.35, 2.7, 2.9],
    )

    add_heading(doc, "Execution and approval controls", 1)
    add_bullet(doc, "Scientific and visual semantic parameters require current task approval unless the user already supplied the exact value.")
    add_bullet(doc, "Presentation and routine technical parameters are selected automatically and recorded for reproducibility.")
    add_bullet(doc, "GPU use more than 64 GiB of memory more than six hours or estimated compute above the configured cost gate requires approval.")
    add_bullet(doc, "Generated code must parse three typed paths and seed arguments and must implement an import safe main entry point and preflight mode.")
    add_bullet(doc, "The Docker executor uses no network a read only root dropped capabilities no new privileges an unprivileged user and a bounded temporary filesystem.")
    add_bullet(doc, "Cloud workers receive an immutable execution bundle and publish result pointers and checksummed artifacts rather than changing control plane state directly.")

    add_heading(doc, "Prompt revision priorities", 1)
    p = doc.add_paragraph(
        "The current design has strong separation of scientific authorization execution and public "
        "communication. The revisions below would make the prompt system easier to reason about and "
        "more faithful to its stated controls."
    )
    add_table(
        doc,
        ["Priority", "Revision", "Reason and proposed change"],
        [
            ["1", "Define source precedence for planner version 2", "State explicitly that the raw current request and exact current task approvals control scope; structured parsing and verified metadata clarify them; prior history and memory are advisory only. Add conflict behavior that asks for clarification instead of guessing."],
            ["1", "Make tool review evidence real", "The deterministic resolver currently marks every matched invocation review complete from registry metadata even when it has not loaded a reviewed parameter manifest. Require a versioned reviewed manifest in the tool registry or leave review complete false."],
            ["1", "Version and hash prompts", "Persist a prompt bundle version and instruction hash on every model stage. Current input hashes mainly cover task data and context, so a prompt edit can change behavior without an explicit plan revision."],
            ["2", "Separate the two Conversation Agent names", "Rename them Intake Parser and User Update Writer so logs model metrics and failures identify the correct boundary."],
            ["2", "Align the unknown tool scout prompt with its payload", "The version 2 scout receives only the master plan while its instructions refer to approved metadata in supplied context. Give it the relevant registry snapshot or rewrite it as a discovery only prompt."],
            ["2", "Minimize context identifiers", "Remove project member lists internal task IDs and other values that do not improve planning. Keep them in the audit manifest rather than the model prompt."],
            ["2", "Clarify response ownership", "Internal plan objects contain response text even though the public writer owns user communication. Prefer typed status fields and let one presentation layer generate user facing prose."],
            ["3", "Deduplicate shared rules", "Output allowlist no Git no network and approval rules are repeated across prompts. Generate agent prompts from tested shared policy fragments to reduce drift while keeping role specific instructions short."],
            ["3", "Add prompt regression evaluations", "Create fixed cases for prompt injection missing scientific values explicit user settings minimal outputs unknown tools code repair and public data leakage. Assert typed fields and approval behavior rather than exact prose."],
            ["3", "Separate model input from audit context", "Keep the full provenance manifest for audit but send only decision relevant fields to each agent. Figure and code workers already follow this narrower payload pattern."],
        ],
        [0.7, 2.0, 4.25],
    )

    add_heading(doc, "Suggested next revision sequence", 1)
    add_number(doc, 1, "Freeze the existing behavior with prompt and policy regression tests.")
    add_number(doc, 2, "Add prompt bundle versions and instruction hashes to TaskStage records.")
    add_number(doc, 3, "Fix tool review provenance and the unknown tool scout payload before changing prompt wording.")
    add_number(doc, 4, "Rewrite the version 2 master prompt with explicit source precedence and conflict handling.")
    add_number(doc, 5, "Reduce context fields and rename the intake and output agents.")
    add_number(doc, 6, "Evaluate approval count output minimality scientific fidelity and data leakage against the frozen test set.")

    add_heading(doc, "Questions for review", 1)
    add_bullet(doc, "Should Mica ask for every interpretation changing value or may a project define reusable reviewed defaults that still require one task level confirmation")
    add_bullet(doc, "Should tool parameter manifests be curated during tool approval or derived again for every task")
    add_bullet(doc, "Should the master plan produce any user prose or only typed state for the public writer")
    add_bullet(doc, "Is local persistence needed immediately or is restart loss acceptable during the current development phase")
    add_bullet(doc, "Which workflows must work locally such as Slack Gmail cloud dataset access or tool builds")

    add_heading(doc, "Source map", 2)
    add_source(doc, "Runtime and cloud  README.md  docker-compose.yml  .env.example  docs/architecture.md  docs/deployment.md  infra/main.tf  infra/variables.tf")
    add_source(doc, "Prompts and context  src/mica/agents/harness.py:42  src/mica/agents/context.py:19  src/mica/connectors/presentation.py:25")
    add_source(doc, "Orchestration and planning  src/mica/services/tasks.py:437  src/mica/services/planning.py:29  src/mica/services/policy.py:144")
    add_source(doc, "Execution and memory  src/mica/execution/backends.py  docker/Dockerfile.science  src/mica/agents/memory.py  docs/memory.md")
    add_source(doc, "Live verification  localhost port 8080  Docker daemon  saved Cloud Run endpoint  28 September 2026")

    props = doc.core_properties
    props.title = "Mica Current Agent Setup Specification"
    props.subject = "Current runtime deployment prompt and agent architecture review"
    props.author = "Mica repository review"
    props.keywords = "Mica agents prompts infrastructure local cloud specification"
    props.created = datetime(2026, 9, 28)
    doc.save(OUTPUT)


if __name__ == "__main__":
    build()
