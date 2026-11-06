// Hosted TSX 面板：状态常驻 + Tabs 二分（可用性 / 发生了什么）。
// 数据链路：Python 侧 @ui.context(id="agent_bridge_panel") -> props.state（见 bridge/sentinel_core/panel.py）
// 动作链路：@ui.action -> props.actions；执行入口仍走 ActionButton / api.call。
//
// 关于注释里不能出现的字样：宿主的 tsxRuntime 用正则 /\bexport\s+default\s+/ 改默认导出，
// 所以本文件任何注释与字符串都不写那六个字母加空格的组合。
import {
  ActionButton,
  ActionForm,
  Alert,
  Button,
  Card,
  DataTable,
  Divider,
  EmptyState,
  Heading,
  Inline,
  KeyValue,
  List,
  Page,
  RefreshButton,
  ScrollArea,
  Stack,
  StatCard,
  StatusBadge,
  Tabs,
  Text,
  useState,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps, Tone } from "@neko/plugin-ui"

// ---------------------------------------------------------------------------
// 类型：与 bridge/sentinel_core/panel.py 的返回契约一一对应
// ---------------------------------------------------------------------------

type EntryRow = {
  id: string
  name: string
  description: string
  timeout: number
  has_params: boolean
  has_required?: boolean
}

type SentinelBlock = {
  state: string
  enabled: boolean
  store_ok: boolean
  l2: boolean
  // 三个 guard 字段分管三件事，别混：
  //   guard         = 命中红线会做什么（alert 只告警 / terminate 真杀 / ""=未知）
  //   guard_enabled = 干预层起没起来
  //   guard_policy  = 身份核验档位
  guard: string
  guard_enabled: boolean
  guard_policy: string
  safepoint: boolean
  webhook: boolean
  timebox_seconds: number
  dropped_events: number
  parse_failures: number
  fix_hint: string
}

type Stats = {
  sessions: number
  events: number
  alerts: number
  blocked: number
  by_severity: Record<string, number>
  by_level: Record<string, number>
  // 「这个数字可能小于真值」——聚合失败且列表顶到扫描上限时的下限标记。
  // 前端只在 events / alerts 上加 ≥，会话和拦截没有截断问题，别乱套。
  partial: boolean
}

type SessionRow = {
  fingerprint: string
  agent: string
  cwd: string
  goal: string
  started_at: number
  ended_at: number | null
  exit_code: number | null
  events: number
  alerts: number
  active: boolean
}

type AlertRow = {
  id: number
  ts: number
  clock: string
  level: string
  title: string
  body: string
  delivered: boolean
  channel: string
}

type TimelineRow = {
  id: number
  ts: number
  clock: string
  kind: string
  severity: string
  level: string
  text: string
  fingerprint: string
}

type State = {
  plugin?: { id?: string; name?: string; version?: string; description?: string }
  entries?: EntryRow[]
  sentinel?: SentinelBlock
  window?: { hours: number; since: number; until: number }
  stats?: Stats
  // 注意：非 "ok" 态时后端给 []；只有「面板数据层整段失败」的外层兜底才给 null。
  sessions?: SessionRow[] | null
  alerts?: AlertRow[] | null
  timeline?: TimelineRow[] | null
  elapsed_ms?: number
}

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

// 慢入口阈值：超过它就走「带 timeoutMs 的 api.call」那条路。
// 以前这里是 `const SLOW = {}` —— 永远为空，慢入口分支永远不可达；而 agent_run
// 声明的超时是 1800 秒，一旦真跑长任务，默认的调用超时会把请求掐掉。
const SLOW_MS = 180000

// 兜底清单只在宿主没给 state 时用（正常路径由 panel.py 生成）。
// 只留 id 与显示名，不手抄 description / has_required / timeout —— 抄本会和真入口漂移。
const FALLBACK_ENTRIES: EntryRow[] = [
  { id: "agents_list", name: "列出可用 Agent", description: "", timeout: 1800, has_params: true, has_required: false },
  { id: "agent_run", name: "派发任务给 Agent", description: "", timeout: 1800, has_params: true, has_required: true },
  { id: "agent_run_parallel", name: "并行派发多个任务", description: "", timeout: 1800, has_params: true, has_required: true },
  { id: "agent_doctor", name: "诊断桥接链路", description: "", timeout: 1800, has_params: true, has_required: false },
]

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

const pad2 = (n: number): string => (n < 10 ? "0" + String(n) : String(n))

// 用 Date 的本地时区格式化；不用 Intl（它虽然可用，但没必要为一个 HH:MM:SS 引入它）。
function clockOf(tsSeconds: number): string {
  if (!tsSeconds || !isFinite(tsSeconds)) return "--:--:--"
  const d = new Date(tsSeconds * 1000)
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds())
}

// 指纹是 uuid hex，整串太长；空串要显式标出来，不能显示成空白（那是「无指纹」而不是「没有」）。
function shortFingerprint(fp: string): string {
  const text = String(fp || "")
  return text ? text.slice(0, 8) : "(无指纹)"
}

// 目录只留末两段，避免长路径把表格挤变形。
function shortPath(p: string): string {
  const text = String(p || "")
  if (!text) return ""
  const parts = text.split(/[\\/]+/).filter((s) => s.length > 0)
  return parts.length <= 2 ? text : ".../" + parts.slice(-2).join("/")
}

function markerFor(level: string): string {
  if (level === "critical") return "!!"
  if (level === "warn") return " !"
  return " ·"
}

function toneOfState(state: string): Tone {
  // 三档刻意不同：no_store 是真故障（在判定、在拦截，但一个字节都没落盘）；
  // runtime_missing 可能只是还没启动完；disabled 是用户自己关的，用 danger 等于骂用户。
  if (state === "no_store") return "danger"
  if (state === "runtime_missing") return "warning"
  if (state === "disabled") return "info"
  return "success"
}

function headlineOfState(state: string): string {
  if (state === "no_store") return "账本不可用"
  if (state === "runtime_missing") return "哨兵未启动"
  if (state === "disabled") return "哨兵已关闭"
  return "哨兵在运行"
}

function labelOfState(state: string): string {
  if (state === "ok") return "正常"
  if (state === "no_store") return "账本故障"
  if (state === "runtime_missing") return "未启动"
  if (state === "disabled") return "已关闭"
  return state
}

function hasParamsOf(e: EntryRow): boolean {
  return typeof e.has_params === "boolean" ? e.has_params : true
}

function isSlow(e: EntryRow): boolean {
  return typeof e.timeout === "number" && e.timeout > SLOW_MS / 1000
}

// 会话状态只按 active 分两档。刻意**不做**「挂了多久算僵死」的推断：
// 那既可能是进程被强杀（真异常），也可能是跑了几小时的构建/训练（完全正常），
// 面板区分不了；而且它得用浏览器时钟去判断账本里的时间戳，两边偏差用户看不见。
// 真要检测僵死会话，正确做法是后端按 pid 查进程是否还活着 —— 那是事实，不是阈值猜测。
function sessionChip(row: SessionRow): { tone: Tone; label: string } {
  // 文案用「未收尾」而不是「进行中」：进程被强杀时 ended_at 也是 NULL，后端分不出来。
  // 「进行中」是在替账本断言一件它不知道的事；「未收尾」只陈述记录里的事实。
  if (row.active) return { tone: "info", label: "未收尾" }
  const code = row.exit_code
  if (code === 0) return { tone: "success", label: "已结束" }
  if (code === null || code === undefined) return { tone: "default", label: "已结束" }
  return { tone: "danger", label: "已结束 · 退出 " + String(code) }
}

// 告警投递状态。刻意不用 DataTable 的「boolean 自动转 StatusBadge」：那个转出来是
// 是/否，而这里要区分三态 —— webhook 发成功、外发全挂只剩本地兜底、还没投递。
function alertChip(row: AlertRow): { tone: Tone; label: string } {
  if (!row.delivered) return { tone: "danger", label: "未投递" }
  if (row.channel === "webhook") return { tone: "success", label: "已发 webhook" }
  if (row.channel === "local") return { tone: "warning", label: "仅本地" }
  return { tone: "success", label: "已投递" }
}

// 「命中红线会做什么」。空字符串表示配置没读到 —— 显示「未知」而不是「已开启干预」，
// 后者会让人误以为有保护。
function guardActionLabel(sentinel: SentinelBlock | undefined): string {
  const action = String((sentinel && sentinel.guard) || "")
  if (!action) return "未知"
  if (action === "alert") return "只告警"
  if (action === "terminate") return "终止进程"
  return action
}

// 「凭什么认人」——后端给的是三个档名，别把 creation_time 这种标识符直接甩给用户。
// none 档下 terminate 一律被拒，这一条必须写出来：用户配了 terminate 却看到「无」，
// 会以为只是没配好，实际是这档下它根本不会生效。
function guardPolicyLabel(sentinel: SentinelBlock | undefined): string {
  const policy = String((sentinel && sentinel.guard_policy) || "")
  if (policy) {
    if (policy === "creation_time") return "进程创建时间"
    if (policy === "time_window") return "时间窗（较弱）"
    if (policy === "none") return "无（terminate 不会生效）"
    return policy
  }
  // 空值有四种真相，别塌成一句「未知」：
  //   降级态 / 未启动 —— 我们确实什么都不知道，只能报「未知」。
  //   干预层没起来   —— doctor()["guard"] 里只有 enabled，压根没有档位可查，报「未启用」。
  //   起来了但读失败 —— runtime.doctor() 对 identity_policy() 是 `except Exception: pass`，
  //                    真抛异常时 policy 键根本不会出现，于是「enabled=True + policy 空」。
  //                    注意这条**今天走不到**：identity_policy() 是全域函数，内部三层兜底
  //                    （探针失败→time_window→none）把异常都消化了，实测拿一个每次访问
  //                    都抛错的 Win API 桩去喂它，仍然返回 time_window 而不是抛。
  //                    留着是给未来的改动上一道保险——那时它会是故障的唯一露头处。
  if (!sentinel || sentinel.state !== "ok") return "未知"
  if (sentinel.guard_enabled === false) return "未启用"
  return "读取失败"
}

// 「下限」标记：聚合统计不完整时，后端给的数字只是「至少有这么多」。
// 含义只有一个 —— **这些数字不能当准数用**。截断与读不到都是它的子集，处置相同。
// 会话、事件、告警都要标：会话数在 stats 缺席时是由同一份被截断的事件列表推导的。
// **拦截不标 ≥**：它只来自 stats，聚合只坏一条计数时它就成 0，写「≥0」没有信息量，
// 所以那一格在部分态下显示「—」（无法确认）。
function countLabel(stats: Stats | undefined, value: number): string {
  return stats && stats.partial ? "≥" + String(value) : String(value)
}

// ---------------------------------------------------------------------------
// 面板
// ---------------------------------------------------------------------------

export default function Panel(props: PluginSurfaceProps<State>) {
  const { state, actions } = props
  const [selectedFp, setSelectedFp] = useState<string>("")

  const sentinel = state.sentinel
  const sentState = sentinel && sentinel.state ? sentinel.state : "runtime_missing"
  const isOk = sentState === "ok"
  const stats = state.stats

  // 列表三态：非 ok 态一律空（后端给 []）；外层兜底给 null，此时连读都没读到。
  const sessions = Array.isArray(state.sessions) ? state.sessions : []
  const alerts = Array.isArray(state.alerts) ? state.alerts : []
  const timeline = Array.isArray(state.timeline) ? state.timeline : []

  const entries = state.entries && state.entries.length ? state.entries : FALLBACK_ENTRIES
  const actionOf = (id: string): HostedAction | undefined =>
    actions.find((a) => a.id === id) as HostedAction | undefined

  const callSlow = (id: string, timeoutMs: number) => {
    props.api.call(id, {}, { userInitiated: true, timeoutMs })
  }

  // 时间线按指纹本地过滤：数据已经在 state 里，不再走 api.call。
  const visibleTimeline = selectedFp
    ? timeline.filter((it) => it.fingerprint === selectedFp)
    : timeline

  // ---- 状态条（常驻，不进 tab） ----
  const degraded =
    isOk &&
    sentinel !== undefined &&
    ((sentinel.dropped_events || 0) > 0 || (sentinel.parse_failures || 0) > 0)

  const ledgerLine = isOk
    ? "账本 " + countLabel(stats, (stats && stats.events) || 0) + " 条事件 · 统计窗 " + String((state.window && state.window.hours) || 12) + " 小时"
    : "读不到账本"

  const statusCard = (
    <Card>
      <Stack gap={12}>
        <Inline justify="space-between">
          <Inline gap={8}>
            <Heading as="h3">{headlineOfState(sentState)}</Heading>
            <StatusBadge tone={toneOfState(sentState)} label={labelOfState(sentState)} />
          </Inline>
          <Text>{ledgerLine}</Text>
        </Inline>
        {isOk ? null : <Alert tone={toneOfState(sentState)} message={(sentinel && sentinel.fix_hint) || "状态未知。"} />}
        {degraded && sentinel !== undefined ? (
          <Alert
            tone="warning"
            message={
              "本次有 " +
              String(sentinel.dropped_events || 0) +
              " 条事件被丢弃、" +
              String(sentinel.parse_failures || 0) +
              " 行解析失败 —— 记录可能不全。"
            }
          />
        ) : null}
      </Stack>
    </Card>
  )

  // ---- 可用性 tab ----
  const layerItems = [
    { label: "L2 判定", value: sentinel && sentinel.l2 ? "开" : "关" },
    { label: "账本", value: sentinel && sentinel.store_ok ? "开" : "关" },
    // 干预动作与干预开关是两件事：guard_enabled 只说明干预层起来了，
    // guard 才说明命中红线时它会不会真的动手（alert 只告警 / terminate 真杀）。
    {
      label: "命中红线",
      value: guardActionLabel(sentinel),
    },
    { label: "干预层", value: sentinel && sentinel.guard_enabled ? "开" : "关" },
    { label: "身份档位", value: guardPolicyLabel(sentinel) },
    { label: "安全点", value: sentinel && sentinel.safepoint ? "开" : "关" },
    { label: "告警通道", value: sentinel && sentinel.webhook ? "webhook + 本地" : "仅本地" },
    { label: "时间盒", value: String((sentinel && sentinel.timebox_seconds) || 0) + " 秒" },
  ]

  const canPanel = (
    <Stack gap={16}>
      <Card title="各层状态">
        <Stack gap={12}>
          <KeyValue items={layerItems} />
          <Divider />
          <Inline justify="space-between">
            <Text>
              {"窗口 " + String((state.window && state.window.hours) || 12) + " 小时内"}
            </Text>
            <RefreshButton label="刷新数据" />
          </Inline>
          {isOk && stats ? (
            <Stack gap={8}>
              <Inline gap={12} wrap>
                <StatCard label="会话" value={countLabel(stats, stats.sessions)} />
                <StatCard label="事件" value={countLabel(stats, stats.events)} />
                <StatCard label="告警" value={countLabel(stats, stats.alerts)} />
                <StatCard label="拦截" value={stats.partial ? "—" : String(stats.blocked)} />
              </Inline>
              {stats.partial ? (
                <Text>{"聚合统计不完整：带 ≥ 的数字是下限，拦截数无法确认。"}</Text>
              ) : null}
            </Stack>
          ) : (
            <Text>数字无意义时不摆数字：先把上面的状态修好。</Text>
          )}
        </Stack>
      </Card>

      <Card title="入口动作">
        <Stack gap={12}>
          {entries.map((e) => {
            const act = actionOf(e.id)
            if (!act) {
              return (
                <Card key={e.id} title={e.name}>
                  <Text>动作未注册(需 @ui.action)。</Text>
                </Card>
              )
            }
            const slow = isSlow(e)
            // 慢入口按入口自己声明的 timeout 放宽调用上限（至少 SLOW_MS）。
            const timeoutMs = Math.max(SLOW_MS, (e.timeout || 0) * 1000)
            const hp = hasParamsOf(e)
            const needsConfirm = e.id === "sentinel_rollback"
            return (
              <Card key={e.id} title={e.name}>
                <Stack gap={8}>
                  {e.description ? <Text>{e.description}</Text> : null}
                  {hp ? (
                    <ActionForm action={act} />
                  ) : (
                    <Stack gap={8}>
                      {slow ? <Text>{"(慢入口:最长可等 " + String(timeoutMs / 1000) + "s)"}</Text> : null}
                      {slow ? (
                        <Button onClick={() => callSlow(e.id, timeoutMs)}>{"执行 " + e.name}</Button>
                      ) : (
                        <ActionButton
                          action={act}
                          confirm={needsConfirm ? "回滚会改动工作区文件，确认执行？" : undefined}
                        >
                          {"执行 " + e.name}
                        </ActionButton>
                      )}
                    </Stack>
                  )}
                </Stack>
              </Card>
            )
          })}
          <Text>带 * 为必填;执行结果以 entry 返回值为准。</Text>
        </Stack>
      </Card>
    </Stack>
  )

  // ---- 发生了什么 tab ----
  const sessionColumns = [
    { key: "fingerprint", label: "会话", render: (row: SessionRow) => shortFingerprint(row.fingerprint) },
    { key: "agent", label: "Agent" },
    { key: "cwd", label: "目录", render: (row: SessionRow) => shortPath(row.cwd) },
    { key: "goal", label: "目标" },
    { key: "started_at", label: "开始", render: (row: SessionRow) => clockOf(row.started_at) },
    { key: "events", label: "事件" },
    { key: "alerts", label: "告警" },
    {
      key: "active",
      label: "状态",
      render: (row: SessionRow) => {
        const chip = sessionChip(row)
        return <StatusBadge tone={chip.tone} label={chip.label} />
      },
    },
  ]

  const alertColumns = [
    { key: "ts", label: "时刻", render: (row: AlertRow) => row.clock || clockOf(row.ts) },
    { key: "level", label: "级别" },
    { key: "title", label: "标题" },
    { key: "body", label: "内容" },
    {
      key: "delivered",
      label: "投递",
      render: (row: AlertRow) => {
        const chip = alertChip(row)
        return <StatusBadge tone={chip.tone} label={chip.label} />
      },
    },
  ]

  // 非 ok 态一律不渲染 DataTable：它的 emptyText 会把「账本坏了」渲染成
  // 「暂无数据」，用户会以为昨晚什么都没发生。这是整个面板的防呆核心。
  const readFailure = (what: string) => (
    <Text>{"读不到" + what + "：" + ((sentinel && sentinel.fix_hint) || "状态未知")}</Text>
  )

  const sessionsSection = (
    <Card title="会话">
      {isOk ? (
        <DataTable
          data={sessions}
          rowKey="fingerprint"
          selectedKey={selectedFp || undefined}
          onSelect={(row: SessionRow) => setSelectedFp(row.fingerprint)}
          emptyText="这段时间还没有会话记录。用「派发任务给 Agent」跑一个。"
          columns={sessionColumns}
        />
      ) : (
        readFailure("会话")
      )}
    </Card>
  )

  const alertsSection = (
    <Card title="告警">
      {isOk ? (
        <DataTable
          data={alerts}
          rowKey="id"
          emptyText="这段时间没有告警记录。"
          columns={alertColumns}
        />
      ) : (
        readFailure("告警")
      )}
    </Card>
  )

  const timelineSection = (
    <Card title={selectedFp ? "时间线（已按会话过滤）" : "时间线"}>
      <Stack gap={8}>
        {selectedFp ? (
          <Inline gap={8}>
            <Text>{"只看会话 " + shortFingerprint(selectedFp)}</Text>
            <Button onClick={() => setSelectedFp("")}>取消过滤</Button>
          </Inline>
        ) : null}
        {isOk ? (
          visibleTimeline.length === 0 ? (
            <EmptyState
              title="这段时间没有事件"
              description={selectedFp ? "这路会话没有留下事件记录。" : "哨兵在运行，只是这段时间没有记录。"}
            />
          ) : (
            <ScrollArea maxHeight="40vh">
              <List
                items={visibleTimeline}
                render={(it: TimelineRow) => (
                  <Text>{it.clock + " " + markerFor(it.level) + " " + it.text}</Text>
                )}
              />
            </ScrollArea>
          )
        ) : (
          readFailure("时间线")
        )}
      </Stack>
    </Card>
  )

  const happenedPanel = (
    <Stack gap={16}>
      {sessionsSection}
      {alertsSection}
      {timelineSection}
    </Stack>
  )

  return (
    <Page
      title="本机 Agent 桥接"
      subtitle="把本机的编码 Agent CLI（CodeBuddy / Claude Code / dsh / omp / EvoX / OpenCode 等）桥接成 N.E.K.O 入口"
    >
      <Stack gap={16}>
        {statusCard}
        <Tabs
          id="agent-bridge"
          items={[
            { id: "can", label: "可用性", content: canPanel },
            { id: "happened", label: "发生了什么", content: happenedPanel },
          ]}
        />
      </Stack>
    </Page>
  )
}
