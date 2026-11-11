// Horncall for opencode 2 (written by `horncall install opencode`; `horncall uninstall opencode` removes it).
// opencode asks in its own window as usual; this plugin also sends the question to your phone through the
// Horncall hook and answers it with the phone's decision. Whichever answer comes first counts: an answer in
// opencode takes the card off the phone. Nothing is changed in your permission rules.
import { spawn } from "node:child_process"
import { appendFileSync, mkdirSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const COMMAND: string[] = __HORNCALL_COMMAND__ // [python, launcher] or [horncall], filled in at install
const AGENT = "opencode"

type Request = {
  id: string
  sessionID: string
  action: string
  resources?: readonly string[]
  metadata?: Record<string, unknown>
}

function run(args: string[], input: unknown, onDone?: (stdout: string) => void) {
  const child = spawn(COMMAND[0], [...COMMAND.slice(1), ...args], { stdio: ["pipe", "pipe", "ignore"], windowsHide: true })
  let out = ""
  child.stdout?.on("data", (chunk) => (out += chunk))
  child.on("close", () => onDone?.(out))
  child.on("error", () => onDone?.(""))
  child.stdin?.on("error", () => {}) // the hook may exit before reading (mode off): not an error
  child.stdin?.end(JSON.stringify(input))
  return child
}

export default {
  id: "horncall",
  setup(ctx: any) {
    const cwd: string | undefined = ctx.location?.directory
    const waiting = new Map<string, { child: ReturnType<typeof spawn>; payload: unknown }>()
    const ours = new Set<string>() // requests we answered ourselves: their "replied" event needs nothing
    const controller = new AbortController()

    const payload = (r: Request) => ({
      hook_event_name: "permission.asked", request_id: r.id, session_id: r.sessionID, cwd,
      tool_name: r.action, tool_input: { resources: r.resources ?? [], metadata: r.metadata ?? {} },
    })

    const ask = (r: Request) => {
      const input = payload(r)
      const child = run(["hook", AGENT], input, (stdout) => {
        if (!waiting.delete(r.id)) return // answered in opencode meanwhile
        let decision: string | undefined
        try {
          decision = JSON.parse(stdout || "{}").decision
        } catch {}
        if (decision !== "allow" && decision !== "deny") return // no opinion: the question stays in opencode
        ours.add(r.id)
        ctx.permission.reply({ sessionID: r.sessionID, requestID: r.id, decision: decision === "allow" ? "once" : "reject" })
          .catch(() => {})
      })
      waiting.set(r.id, { child, payload: input })
    }

    const settledHere = (requestID: string) => {
      if (ours.delete(requestID)) return
      const entry = waiting.get(requestID)
      if (!entry) return
      waiting.delete(requestID)
      run(["posttool", AGENT], entry.payload) // the phone drops the card
      entry.child.kill()
    }

    // Capture mode (HORNCALL_CAPTURE=1, docs/AGENTS.md): samples of the events that may carry opencode's own
    // questions to the user (its `question` tool) — 2.2.5 has no question.asked; the shape is not documented.
    const capturing = process.env.HORNCALL_CAPTURE === "1"
    const capture = (event: any) => {
      try {
        const dir = join(process.env.HORNCALL_HOME || join(homedir(), ".horncall"), "captures", AGENT)
        mkdirSync(dir, { recursive: true })
        appendFileSync(join(dir, "events.jsonl"), JSON.stringify({ t: new Date().toISOString(), event }) + "\n")
      } catch {}
    }

    // opencode's own questions to the user (its `question` tool): 2.2.5 has no question event, but the tool call
    // shows in session.tool.called (no tool name there; its input has `questions`). A card for the phone, answered
    // in opencode; the tool's end takes the card off.
    const questions = new Map<string, unknown>()
    const questionAsked = (data: any) => {
      const input = {
        hook_event_name: "question.asked", request_id: data.id, session_id: data.sessionID, cwd,
        tool_name: "question", tool_input: data.input,
      }
      questions.set(String(data.id), input)
      run(["hook", AGENT], input)
    }
    const questionDone = (id: string) => {
      const input = questions.get(id)
      if (!input) return
      questions.delete(id)
      run(["posttool", AGENT], input)
    }

    // The plugin was loaded twice in one opencode (every event came twice, 2026-11-11): handle each event once.
    const seen: Set<string> = ((globalThis as any).__horncallSeen ??= new Set<string>())
    const firstTime = (id: unknown) => {
      if (typeof id !== "string") return true
      if (seen.has(id)) return false
      seen.add(id)
      if (seen.size > 500) seen.delete(seen.values().next().value as string)
      return true
    }

    const busy = new Set<string>()
    void (async () => {
      try {
        // No `types` filter: with one, 2.2.5 delivered nothing (seen 2026-11-05).
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          if (!firstTime(event.id)) continue
          const data = event.data ?? {}
          if (capturing && /^session\.(form\.|tool\.|inbox\.enqueued)/.test(String(event.type))) capture(event)
          if (event.type === "session.tool.called" && Array.isArray(data.input?.questions)) questionAsked(data)
          else if (/^session\.tool\.(success|failed|interrupted)$/.test(String(event.type))) questionDone(String(data.id))
          if (event.type === "permission.asked") ask(data as Request)
          else if (event.type === "permission.replied") settledHere(String(data.requestID))
          else if (event.type === "session.execution.started" && !busy.has(data.sessionID)) {
            // A turn begins / ends (2.2.5 sends these, not session.status).
            busy.add(data.sessionID)
            run(["event", AGENT, "working"], { session_id: data.sessionID, cwd })
          } else if (String(event.type).startsWith("session.execution.") && busy.delete(data.sessionID)) {
            run(["event", AGENT, "idle"], { session_id: data.sessionID, cwd })
          }
        }
      } catch {}
    })()

    return () => {
      controller.abort()
      for (const { child } of waiting.values()) child.kill()
    }
  },
}
