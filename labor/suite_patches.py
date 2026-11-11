"""Labor Budget page changes that keep it in step with the rest of the suite (used by labor/build.py).

labor/sync.py does the work on the server. The suite's lines are its own: a production line added here starts a
machine in the suite (with this plan's shifts and lunch) when budget A is saved; after that the plan's shifts and
lunch per line are only the plan's, and the Scheduler's Lines & Shifts shows where they differ. A machine's name,
stage, color and speed come from the suite (Admin / Scheduler) and are locked here. Crew names are offered from the Team list. New styles are inline, since labor.css is built from the calculator's own classes.
"""
import json

FROM_SUITE = "A machine setting: set in Admin › Machines or the Scheduler (Lines & Shifts)."
FROM_SUITE_NAME = "This line is a machine in the suite: rename it or change its stage in Admin › Machines or the Scheduler (Lines & Shifts)."
BADGE = "font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;white-space:nowrap;"

PATCHES = [
    # Stage 3 (Arts & Crafts) and Glue Bead machines: their crews are labor cost; their filters are already
    # counted as units at Stage 1 and Stage 2, so they add no units of their own.
    ("the stage list",
     "const STAGES = ['Stage 1', 'Stage 2', 'Packaging Only', 'Non Production', 'Backup'];",
     "const STAGES = ['Stage 1', 'Stage 2', 'Stage 3', 'Glue Bead', 'Packaging Only', 'Non Production', 'Backup'];\n"
     "    const FROM_SUITE_TEXT = " + json.dumps(FROM_SUITE) + ";\n"
     "    const LOCK_ATTR = 'style=\"opacity:.6;cursor:not-allowed\" title=\"' + FROM_SUITE_TEXT.replace(/&/g, '&amp;') + '\"';\n"
     "    const LOCK_NAME = 'style=\"opacity:.6;cursor:not-allowed\" title=\"' + " + json.dumps(FROM_SUITE_NAME) + ".replace(/&/g, '&amp;') + '\"';"),
    ("isLaborOnly()",
     "const isLaborOnly = (stage) => stage === 'Packaging Only' || stage === 'Non Production' || stage === 'Backup';",
     "const isLaborOnly = (stage) => stage === 'Packaging Only' || stage === 'Non Production' || stage === 'Backup' || stage === 'Stage 3' || stage === 'Glue Bead';"),
    ("the line stage defaults",
     "'Stage 1': findId('stage 1') || firstId, 'Stage 2': findId('stage 2') || firstId,",
     "'Stage 1': findId('stage 1') || firstId, 'Stage 2': findId('stage 2') || firstId,\n"
     "        'Stage 3': findId('stage 3') || firstId, 'Glue Bead': findId('stage 3') || firstId,"),
    ("the stage-to-labor map",
     "document.getElementById('line-stage-labor-map').innerHTML = ['Stage 1', 'Stage 2', 'Packaging Only', 'Non Production', 'Backup'].map(",
     "document.getElementById('line-stage-labor-map').innerHTML = STAGES.map("),

    # a line from the suite: its name, color, stage and speed are the suite's (which shifts it runs is changed
    # here or in the Scheduler; either one changes both)
    ("the line color",
     """<input type="color" value="${line.color || '#2563eb'}" onchange="updateLine(${lIdx}, 'color', this.value)\"""",
     """<input type="color" value="${line.color || '#2563eb'}" ${line.machineId ? 'disabled ' + LOCK_ATTR : ''} onchange="updateLine(${lIdx}, 'color', this.value)\""""),
    ("the line name",
     """<input type="text" value="${esc(line.name)}" onchange="updateLine(${lIdx}, 'name', this.value)\"""",
     """<input type="text" value="${esc(line.name)}" ${line.machineId ? 'readonly ' + LOCK_NAME : ''} onchange="updateLine(${lIdx}, 'name', this.value)\""""),
    ("the line stage",
     """<select onchange="updateLine(${lIdx}, 'stage', this.value)" class="bg-white font-bold text-xs text-slate-800 py-1.5 px-3">""",
     """<select ${line.machineId ? 'disabled ' + LOCK_NAME : ''} onchange="updateLine(${lIdx}, 'stage', this.value)" class="bg-white font-bold text-xs text-slate-800 py-1.5 px-3">"""),
    ("the line badges",
     "                ${thresholdLineBadge(line.id)}\n                ${collapsed ? ",
     "                ${thresholdLineBadge(line.id)}\n"
     "                ${line.machineId ? `<span style=\"" + BADGE + "background:#e0f2fe;color:#075985\" title=\"A machine in the Scheduler and the Operator Kiosk. Its name, stage, color and speed are the suite's; the shifts and lunch here are this plan's (the Scheduler's Lines & Shifts shows where they differ).\">Machine${line.inService === false ? ' · out of service' : ''}</span>`\n"
     "                  : line.suiteMissing ? `<span style=\"" + BADGE + "background:#fee2e2;color:#991b1b\" title=\"Save the plan to add this line to the Scheduler and Admin, with this plan's shifts and lunch to start.\">New machine</span>` : ''}\n"
     "                ${collapsed ? "),
    ("Delete Line",
     """<button onclick="removeLine(${lIdx})" class="text-xs text-red-600""",
     """<button onclick="removeLine(${lIdx})" ${line.machineId ? 'title="Removes it from this plan only: the machine stays in the suite (take it out of service in Admin › Machines)"' : ''} class="text-xs text-red-600"""),
    ("the line speed",
     """<input type="number" min="0" step="1" value="${line.hourlyRate}" onchange="updateLine(${lIdx}, 'hourlyRate', Number(this.value))" class="w-full bg-white">""",
     """<input type="number" min="0" step="1" value="${line.hourlyRate}" ${line.machineId ? 'readonly ' + LOCK_ATTR : ''} onchange="updateLine(${lIdx}, 'hourlyRate', Number(this.value))" class="w-full bg-white">
                ${line.machineId && line.rateBasis ? `<div style="font-size:11px;color:#64748b;margin-top:3px">${esc(line.rateBasis)}</div>` : ''}"""),
    ("Copy Line",
     "          clone.id = 'L' + Date.now();\n          clone.name = newName.trim();",
     "          clone.id = 'L' + Date.now();\n          clone.name = newName.trim();\n"
     "          delete clone.machineId; delete clone.rateBasis;   // a copy is the plan's own line, not the suite's machine"),
    # a new line starts labor-only; picking a production stage (Stage 1, 2, 3, Glue Bead) makes it a machine when saved
    ("addLine()",
     "        name: `Line ${state.lines.length + 1} - New Line (Stage 1)`,\n        stage: 'Stage 1',",
     "        name: `Line ${state.lines.length + 1} - Labor only`,\n        stage: 'Non Production',"),

    # shifts: how many, and each shift's main crew schedule (A), are the suite's shifts: changed here, followed there
    ("schedule start",
     """<input type="time" value="${s.startTime}" onchange="updateSchedule(${idx}, 'startTime', this.value)\"""",
     """<input type="time" value="${s.startTime}" onchange="updateSchedule(${idx}, 'startTime', this.value)\""""),
    ("schedule end",
     """<input type="time" value="${s.endTime}" onchange="updateSchedule(${idx}, 'endTime', this.value)\"""",
     """<input type="time" value="${s.endTime}" onchange="updateSchedule(${idx}, 'endTime', this.value)\""""),
    ("schedule lunch",
     """<input type="number" min="0" max="180" value="${s.lunchMins}" onchange="updateSchedule(${idx}, 'lunchMins', Number(this.value))\"""",
     """<input type="number" min="0" max="180" value="${s.lunchMins}" onchange="updateSchedule(${idx}, 'lunchMins', Number(this.value))\""""),
    ("schedule on/off",
     """<input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="updateSchedule(${idx}, 'enabled', this.checked)\"""",
     """<input type="checkbox" ${s.enabled ? 'checked' : ''} ${s.suiteShift ? 'disabled ' + LOCK_ATTR : ''} onchange="updateSchedule(${idx}, 'enabled', this.checked)\""""),
    ("schedule delete", """<button onclick="deleteSchedule(${idx})" class=\"""", """<button onclick="deleteSchedule(${idx})" ${s.suiteShift ? 'hidden' : ''} class=\""""),
    ("schedule days",
     """<button type="button" onclick="toggleScheduleDay(${idx}, ${dow})" class=\"""",
     """<button type="button" onclick="toggleScheduleDay(${idx}, ${dow})" class=\""""),
    ("the schedule badge",
     """Shift ${s.shift} &bull; ${esc(s.code)}</span>""",
     """Shift ${s.shift} &bull; ${esc(s.code)}</span>${s.suiteShift ? `<span style=\"""" + BADGE + """background:#e0f2fe;color:#075985\" title=\"${FROM_SUITE_TEXT}\">${esc(s.suiteName || 'Suite shift')}</span>` : ''}"""),

    # crew names: offered from the Team list; a name not on it is outlined
    ("the crew name",
     """<input type="text" value="${esc(slot.name)}" placeholder="Type employee name..." onchange="updateSlotField""",
     """<input type="text" list="suite-people" value="${esc(slot.name)}" placeholder="Type employee name..." ${slot.name && state.suite && (state.suite.notOnTeam || []).includes(String(slot.name).trim()) ? 'style="border-color:#dc2626" title="Not on the Team list (Operator Kiosk › Team)"' : ''} onchange="updateSlotField"""),
]


def apply(html, patch):
    for what, old, new, *n in PATCHES:
        html = patch(html, old, new, what, *n)
    return html
