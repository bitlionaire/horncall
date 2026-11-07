// Horncall for opencode 2 (written by `horncall install opencode`; `horncall uninstall opencode` removes it).
// opencode asks in its own window as usual; this plugin also sends the question to your phone through the
// Horncall hook and answers it with the phone's decision. Whichever answer comes first counts: an answer in
// opencode takes the card off the phone. Nothing is changed in your permission rules.
import { spawn } from "node:child_process"

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

    const busy = new Set<string>()
    void (async () => {
      try {
        // No `types` filter: with one, 2.2.5 delivered nothing (seen 2026-11-05).
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const data = event.data ?? {}
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
