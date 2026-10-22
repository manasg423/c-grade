import os
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Preformatted,
    KeepTogether
)

def create_setup_pdf(filename="C_Grade_Ubuntu_Setup_Guide.pdf"):
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=4
    )
    
    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#475569'),
        spaceAfter=14
    )

    h1_style = ParagraphStyle(
        'PhaseHeading',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#1e3a8a'),
        spaceBefore=10,
        spaceAfter=6
    )

    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#1e293b')
    )

    bold_label = ParagraphStyle(
        'BoldLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#0f172a')
    )

    code_style = ParagraphStyle(
        'CodeStyle',
        fontName='Courier',
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor('#0f172a')
    )

    table_header_style = ParagraphStyle(
        'THStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.white
    )

    table_cell_style = ParagraphStyle(
        'TDStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor('#1e293b')
    )

    table_code_style = ParagraphStyle(
        'TDCodeStyle',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#0f172a')
    )

    def code_box(code_text):
        p = Preformatted(code_text.strip(), code_style)
        t = Table([[p]], colWidths=[540])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f1f5f9')),
            ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#cbd5e1')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        return t

    story = []

    # Title & Metadata
    story.append(Paragraph("C-Grade: Ubuntu Lab Deployment & Setup Guide", title_style))
    story.append(Paragraph("Offline C Grading System • Zero-GUI Terminal Execution Guide", subtitle_style))

    # Phase 1
    story.append(Paragraph("Phase 1: System Packages & Hardware Profiling", h1_style))
    story.append(Paragraph("Install compilers, sanitizers, Valgrind, and Linux performance profiling tools:", body_style))
    story.append(Spacer(1, 4))
    story.append(code_box(
        "sudo apt update && sudo apt install -y \\\n"
        "  build-essential gcc cppcheck valgrind \\\n"
        "  linux-tools-common linux-tools-generic \\\n"
        "  python3-pip python3-venv curl git"
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Enable hardware CPU performance counter monitoring (for complexity profiling):", body_style))
    story.append(Spacer(1, 2))
    story.append(code_box("sudo sysctl -w kernel.perf_event_paranoid=1"))
    story.append(Spacer(1, 6))

    # Phase 2
    story.append(Paragraph("Phase 2: Install & Configure Local Ollama (LLM Review)", h1_style))
    story.append(Paragraph("Set up the local model daemon for code readability analysis (qwen3.5:2b):", body_style))
    story.append(Spacer(1, 4))
    story.append(code_box(
        "# 1. Install Ollama service\n"
        "curl -fsSL https://ollama.com/install.sh | sh\n\n"
        "# 2. Start and enable system daemon\n"
        "sudo systemctl enable --now ollama\n\n"
        "# 3. Pull model weights\n"
        "ollama pull qwen3.5:2b\n\n"
        "# 4. Warm up weights into RAM\n"
        "ollama run qwen3.5:2b \"Say hello in one word\""
    ))
    story.append(Spacer(1, 6))

    # Phase 3
    story.append(Paragraph("Phase 3: Repository Setup & Python Environment", h1_style))
    story.append(Paragraph("Create a native virtual environment linked to the lab machine's Python runtime:", body_style))
    story.append(Spacer(1, 4))
    story.append(code_box(
        "# 1. Clone repository\n"
        "git clone <YOUR_GITHUB_REPO_URL>\n"
        "cd <YOUR_REPO_FOLDER_NAME>\n\n"
        "# 2. Build local venv (never copy across machines)\n"
        "python3 -m venv venv\n"
        "source venv/bin/activate\n\n"
        "# 3. Install direct dependencies\n"
        "pip install --upgrade pip\n"
        "pip install requests streamlit"
    ))
    story.append(Spacer(1, 6))

    # Phase 4
    story.append(Paragraph("Phase 4: Run Verification Tests in Terminal", h1_style))
    story.append(Paragraph("Execute the specialist analyzers on the test submission:", body_style))
    story.append(Spacer(1, 4))
    story.append(code_box(
        "# I/O Black-box & Auto-generation Test:\n"
        "python test_tool.py --source submissions/str_len.c --analyzer io_tests --rules rules/str_rules.json\n\n"
        "# AddressSanitizer, UBSan, and Memory Leak Check:\n"
        "python test_tool.py --source submissions/str_len.c --analyzer sanitizer_run --rules rules/str_rules.json\n\n"
        "# Edge Case & Oracle Counter-Example Verification:\n"
        "python test_tool.py --source submissions/str_len.c --analyzer edge_case_runner --rules rules/str_rules.json\n\n"
        "# Hardware Instruction & Complexity Profiler:\n"
        "python test_tool.py --source submissions/str_len.c --analyzer complexity_estimator --rules rules/str_rules.json\n\n"
        "# Local LLM Deterministic Style Review (qwen3.5:2b):\n"
        "python test_tool.py --source submissions/str_len.c --analyzer llm_review --rules rules/str_rules.json"
    ))
    story.append(Spacer(1, 6))

    # Phase 5
    story.append(Paragraph("Phase 5: Viewing Reports & Launching UI", h1_style))
    story.append(Paragraph("Inspect generated audit logs or start the web dashboard:", body_style))
    story.append(Spacer(1, 4))
    story.append(code_box(
        "# View plain-text human summary:\n"
        "cat reports/str_len.txt\n\n"
        "# View structured JSON audit log:\n"
        "cat reports/str_len.json\n\n"
        "# Optional: Start web interface (open http://localhost:8501 in browser):\n"
        "streamlit run ui.py"
    ))
    story.append(Spacer(1, 8))

    # Phase 6: Troubleshooting Table
    story.append(Paragraph("Phase 6: Lab Troubleshooting Quick Reference", h1_style))
    
    table_data = [
        [
            Paragraph("Issue Encountered", table_header_style),
            Paragraph("Root Cause", table_header_style),
            Paragraph("Terminal Command Fix", table_header_style)
        ],
        [
            Paragraph("bash: ./venv/bin/python: No such file", table_cell_style),
            Paragraph("Copied venv folder from personal machine", table_cell_style),
            Paragraph("rm -rf venv && python3 -m venv venv && source venv/bin/activate && pip install requests streamlit", table_code_style)
        ],
        [
            Paragraph("pkill: Operation not permitted", table_cell_style),
            Paragraph("Ollama running as root system daemon", table_cell_style),
            Paragraph("sudo systemctl restart ollama", table_code_style)
        ],
        [
            Paragraph("llm_review uses heuristic fallback", table_cell_style),
            Paragraph("Ollama offline or model not loaded", table_cell_style),
            Paragraph("ollama run qwen3.5:2b \"hi\" && curl http://127.0.0.1:11434/api/tags", table_code_style)
        ],
        [
            Paragraph("perf: Permission denied", table_cell_style),
            Paragraph("Kernel event paranoid setting active", table_cell_style),
            Paragraph("sudo sysctl -w kernel.perf_event_paranoid=1", table_code_style)
        ],
        [
            Paragraph("valgrind: command not found", table_cell_style),
            Paragraph("Package omitted during apt setup", table_cell_style),
            Paragraph("sudo apt install -y valgrind", table_code_style)
        ]
    ]

    t = Table(table_data, colWidths=[130, 150, 260])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e293b')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#94a3b8')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    
    story.append(t)
    doc.build(story)
    print(f"Successfully generated: {os.path.abspath(filename)}")

if __name__ == "__main__":
    create_setup_pdf()