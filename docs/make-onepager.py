# SPDX-License-Identifier: GPL-3.0-or-later
"""Build ROUTING-onepager.pdf, the one-page summary of ROUTING.md.

    uv run --with reportlab python docs/make-onepager.py ROUTING-onepager.pdf

Needs the Noto Sans and DejaVu Sans fonts (Debian/Ubuntu: fonts-noto-core, fonts-dejavu-core).
Keep it in step with ROUTING.md when the design changes.
"""
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    FrameBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

NOTO = '/usr/share/fonts/truetype/noto/'
DEJAVU = '/usr/share/fonts/truetype/dejavu/'
pdfmetrics.registerFont(TTFont('Sans', NOTO + 'NotoSans-Regular.ttf'))
pdfmetrics.registerFont(TTFont('Sans-B', NOTO + 'NotoSans-Bold.ttf'))
pdfmetrics.registerFont(TTFont('Sans-I', NOTO + 'NotoSans-Italic.ttf'))
pdfmetrics.registerFont(TTFont('DV', DEJAVU + 'DejaVuSans.ttf'))  # arrows
pdfmetrics.registerFont(TTFont('Mono', DEJAVU + 'DejaVuSansMono.ttf'))
registerFontFamily('Sans', normal='Sans', bold='Sans-B', italic='Sans-I', boldItalic='Sans-B')

INK = colors.HexColor('#1B2430')
MUTED = colors.HexColor('#5A6573')
ACC = colors.HexColor('#0F766E')
ACC_BG = colors.HexColor('#E6F2F0')
RULE = colors.HexColor('#D5DBE1')
WARN = colors.HexColor('#9A3412')
ARROW = '<font name="DV">→</font>'

W, H = A4
M = 13 * mm
GAP = 7 * mm

body = ParagraphStyle('body', fontName='Sans', fontSize=8.1, leading=10.6, textColor=INK)
small = ParagraphStyle('small', parent=body, fontSize=7.6, leading=9.8)
h2 = ParagraphStyle('h2', fontName='Sans-B', fontSize=10, leading=12.4, textColor=ACC,
                    spaceBefore=6, spaceAfter=2.5)
bullet = ParagraphStyle('bullet', parent=body, leftIndent=8, bulletIndent=0)
cell = ParagraphStyle('cell', parent=small)
cellb = ParagraphStyle('cellb', parent=small, fontName='Sans-B')


def P(t, s=body):
    return Paragraph(t, s)


def B(t):
    return Paragraph(t, bullet, bulletText='•')


def table(rows, widths, head=False):
    data = [[P(c, cellb if (head and i == 0) or j == 0 else cell) for j, c in enumerate(r)]
            for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths)
    st = [('VALIGN', (0, 0), (-1, -1), 'TOP'),
          ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
          ('TOPPADDING', (0, 0), (-1, -1), 1.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 1.6),
          ('LINEBELOW', (0, 0), (-1, -1), 0.4, RULE)]
    if head:
        st.append(('BACKGROUND', (0, 0), (-1, 0), ACC_BG))
    t.setStyle(TableStyle(st))
    return t


class Flow(Flowable):
    """The decision pipeline: boxes, arrows and the loop back for the next attempt."""
    steps = (
        ('Task facts', 'kind, specification,\nchecks, scope, stakes'),
        ('Tier', 'rules F1\u2013F8 on facts;\nscorer fills unknowns'),
        ('Route  or  wait', 'rules RT1\u2013RT7 on\ncapacity and fit'),
        ('Launch', 'harness via gateway\n(LiteLLM) or directly'),
        ('Outcome', 'accepted, or a\nclassified failure'),
    )
    highlight = ('Tier', 'Route  or  wait')

    def __init__(self, width):
        super().__init__()
        self.width, self.height = width, 25 * mm

    def draw(self):
        c = self.canv
        n = len(self.steps)
        aw = 5.5 * mm
        bw = (self.width - (n - 1) * aw) / n
        bh = 15.5 * mm
        y = self.height - bh
        for i, (t, sub) in enumerate(self.steps):
            x = i * (bw + aw)
            hl = t in self.highlight
            c.setFillColor(ACC if hl else ACC_BG)
            c.setStrokeColor(ACC)
            c.setLineWidth(0.8)
            c.roundRect(x, y, bw, bh, 2.2 * mm, fill=1, stroke=1)
            c.setFillColor(colors.white if hl else INK)
            c.setFont('Sans-B', 8.6)
            c.drawCentredString(x + bw / 2, y + bh - 5 * mm, t)
            c.setFont('Sans', 6.9)
            c.setFillColor(colors.white if hl else MUTED)
            for k, line in enumerate(sub.split('\n')):
                c.drawCentredString(x + bw / 2, y + bh - 8.6 * mm - k * 3 * mm, line)
            if i < n - 1:
                ax, ay = x + bw + 0.8 * mm, y + bh / 2
                c.setStrokeColor(ACC)
                c.setLineWidth(1.1)
                c.line(ax, ay, ax + aw - 2.2 * mm, ay)
                p = c.beginPath()
                p.moveTo(ax + aw - 1.6 * mm, ay)
                p.lineTo(ax + aw - 3 * mm, ay + 1.1 * mm)
                p.lineTo(ax + aw - 3 * mm, ay - 1.1 * mm)
                p.close()
                c.setFillColor(ACC)
                c.drawPath(p, fill=1, stroke=0)
        x_out = (n - 1) * (bw + aw) + bw / 2
        x_back = 1 * (bw + aw) + bw / 2
        yl = y - 4.2 * mm
        c.setStrokeColor(WARN)
        c.setLineWidth(0.9)
        c.setDash(2, 1.5)
        c.line(x_out, y, x_out, yl)
        c.line(x_out, yl, x_back, yl)
        c.line(x_back, yl, x_back, y - 1.6 * mm)
        c.setDash()
        p = c.beginPath()
        p.moveTo(x_back, y - 0.2 * mm)
        p.lineTo(x_back - 1.1 * mm, y - 1.8 * mm)
        p.lineTo(x_back + 1.1 * mm, y - 1.8 * mm)
        p.close()
        c.setFillColor(WARN)
        c.drawPath(p, fill=1, stroke=0)
        c.setFont('DV', 6.8)
        c.drawCentredString((x_out + x_back) / 2, yl - 3.2 * mm,
                            'next attempt: facts are re-read and the tier recomputed; '
                            'every decision is logged and can be replayed')


def header(c, doc):
    c.saveState()
    c.setFillColor(INK)
    c.setFont('Sans-B', 15)
    c.drawString(M, H - M - 5 * mm, 'hive-route')
    tw = pdfmetrics.stringWidth('hive-route', 'Sans-B', 15)
    c.setFont('Sans', 15)
    c.setFillColor(MUTED)
    c.drawString(M + tw + 3 * mm, H - M - 5 * mm, 'a slim model router for One Hive (R8)')
    c.setFont('Sans', 7.4)
    c.drawRightString(W - M, H - M - 5 * mm, 'rev 8 · 6 Oct 2026 · details in ROUTING.md')
    c.setStrokeColor(ACC)
    c.setLineWidth(1.4)
    c.line(M, H - M - 8.2 * mm, W - M, H - M - 8.2 * mm)
    c.restoreState()


top_h = 46 * mm
top = Frame(M, H - M - 9 * mm - top_h, W - 2 * M, top_h, id='top',
            leftPadding=0, rightPadding=0, topPadding=2, bottomPadding=0)
cw = (W - 2 * M - GAP) / 2
col_h = H - M - 9 * mm - top_h - M
left = Frame(M, M, cw, col_h, id='l', leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
right = Frame(M + cw + GAP, M, cw, col_h, id='r', leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0)

doc = BaseDocTemplate(sys.argv[1], pagesize=A4, leftMargin=M, rightMargin=M, topMargin=M,
                      bottomMargin=M, title='hive-route: a slim model router for One Hive',
                      subject='R8 routing design, one-page summary')
doc.addPageTemplates([PageTemplate(id='p', frames=[top, left, right], onPage=header)])

s = []
s.append(P('<b>For each unit of work, the router decides which model runs it.</b> It computes the tier the '
           'work needs from checkable facts about the task, then picks a model with capacity in that tier. '
           'Every decision is deterministic, explainable, logged, and recomputed at each attempt. It works '
           'for hives on API keys, subscriptions, local models, or a mix.',
           ParagraphStyle('lead', parent=body, fontSize=9.2, leading=12.2)))
s.append(Spacer(1, 3 * mm))
s.append(Flow(W - 2 * M))
s.append(FrameBreak())

# ---- left column
s.append(P('Deciding the tier', h2))
s.append(P('Tiers: <font name="Mono" size="7">local · light · standard · strong</font>. '
           'The tier is the highest one any rule requires:'))
s.append(Spacer(1, 1.5))
s.append(table([
    ['F1', 'Kind: digest local; work, review light; plan, triage standard; consult strong.'],
    ['F2', 'Only a goal, no plan: standard, and suggest splitting off a plan task.'],
    ['F3', 'Touches many components: standard.'],
    ['F4', 'No check at all: standard.'],
    ['F5', 'Costly or hard to undo, without an independent check: strong.'],
    ['F6', 'Many tasks blocked on it, without an independent check: strong.'],
    ['F7', 'Reviews: at least the author\'s tier.'],
    ['F8', 'Two failed checks: one tier up (at strong, a consult).'],
], [8 * mm, cw - 8 * mm]))
s.append(Spacer(1, 2))
s.append(B('<b>Facts, not verdicts.</b> They come from the task and the record: is there a plan, is there a '
           'check the worker can\'t edit, how much does it touch, what did earlier attempts do.'))
s.append(B('<b>Cheaper means better-specified.</b> The creator\'s hint can only raise the tier. To go cheaper, '
           'add an independent check, write the plan first, or split the task.'))
s.append(B('<b>Scorer.</b> A small fixed model estimates <i>unknown</i> facts only, through the same rules. '
           'Shadow mode first; live once replay shows it helps.'))
s.append(B('<b>Learning.</b> Replay computes success per kind, facts and tier; a person approves each '
           'table change.'))

s.append(P('Choosing the route', h2))
s.append(table([
    ['RT1', 'Operator override wins (recorded).'],
    ['RT2', 'Only qualified routes that fit: pool has capacity, cost fits budget, context and tools fit. '
            'Unknown counts as not fitting.'],
    ['RT3', f'The tier is a <b>floor</b>. Nothing fits now {ARROW} wait; nothing could ever fit {ARROW} '
            'no_route (a table problem). Never a silent downgrade.'],
    ['RT4', 'Reviews use a different model family.'],
    ['RT5', 'After a failure, act on its class (right); a reassignment excludes the last route.'],
    ['RT6', 'Within the tier: listed order, lowest cost, or most headroom.'],
    ['RT7', 'After an interruption, truncation or missing info, keep the route.'],
], [10 * mm, cw - 10 * mm]))

s.append(P('Core ideas', h2))
s.append(B('<b>Route</b>: a named, pinned model config; any change makes a new version, so switches are visible. '
           'Each passes a <b>canary</b> on past tasks, with a recorded lesson; qualification is bound to the pin.'))
s.append(B('<b>Pool</b>: capacity with limits: <b>metered</b> (API money), <b>subscription</b> (usage windows), '
           '<b>local</b> (concurrency).'))
s.append(B('<b>One route per attempt</b> or per episode (a route service for agents with their own loop), re-routed at restarts and checkpoints; <b>scripts, not models</b>, for polling, parsing and tests.'))

# ---- right column
s.append(FrameBreak())
s.append(P('Failures: one remedy per class', h2))
s.append(table([
    ['Class', 'Remedy'],
    ['outage', 'Another route in the same tier, or wait. Never a tier up.'],
    ['capacity', 'Rate limit, window or budget hit: another pool, or wait.'],
    ['truncated', 'Larger limits, or split the task. Not a tier up.'],
    ['missing info', 'Get the information, retry on the same route.'],
    ['failed check', 'Retry once with feedback, then a tier up or a consult.'],
    ['stalled', 'Supervisor or operator decides; a tier up is one option.'],
    ['interrupted', 'Process lost or restarted, not the model\'s fault: same route.'],
    ['indeterminate', 'Unknown whether the call ran: reconcile first, no blind retry.'],
], [23 * mm, cw - 23 * mm], head=True))

s.append(P('Patterns the rules produce', h2))
s.append(B('<b>Plan strong, execute light.</b> A plan task yields a plan and checks; the work tasks are then '
           'well specified and checked, so they run light.'))
s.append(B('<b>Consult.</b> A strong model answers one narrow question, with no tools; a light worker does the work.'))

s.append(P('Gateway: LiteLLM, where a route needs it', h2))
s.append(B('Routes are gateway aliases; agents get a worker key that can only call models, never provider credentials.'))
s.append(B('Spend is logged per pool, route, task and attempt; tag budgets cap each period and each attempt.'))
s.append(B('The route table is the source of truth; gateway fallbacks and hidden retries are off.'))

s.append(P('How it fits One Hive', h2))
s.append(table([
    ['R1 record', 'route.* summaries (amendment A1): decided, waiting, canary, drift, mode, table; '
                  'each bound to the router\'s full, replayable log entry.'],
    ['R5 review', 'Rules F7 and RT4.'],
    ['R6 runtime', 'Asks the router at each attempt start; launches the harness with the answer (examples/launch.sh).'],
    ['R9 replay', 'Replays under another table; evaluates the scorer; proposes tuning.'],
], [19 * mm, cw - 19 * mm]))

s.append(P('Rollout, and what "done" means', h2))
s.append(P(f'<b>Built and live in 1-hive:</b> decide() and its rules; usage from providers\' own '
           'reports and the gateway; canaries, drift checks, the scorer (shadow); a gateway for self-hosted '
           'models and API keys, with budgets per period and per attempt and a worker key; routing on the '
           f'hive record. <b>Adopting:</b> docs/ADOPTING.md: route table {ARROW} launcher in <b>fixed</b> mode '
           f'(the baseline) {ARROW} canaries {ARROW} <b>live</b> {ARROW} tuning. <b>Done:</b> more accepted '
           'tasks per unit of capacity than the baseline, with no drop in review pass rate.'))
s.append(P('Not in v1: the scorer setting tiers directly, automatic tuning, a spend ledger, '
           'mid-attempt switches.', ParagraphStyle('sm2', parent=small, spaceBefore=3, textColor=MUTED)))

doc.build(s)
