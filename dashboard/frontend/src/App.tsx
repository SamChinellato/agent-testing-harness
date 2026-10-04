import React, { useEffect, useState } from 'react';
import '@patternfly/react-core/dist/styles/base.css';
import {
  Page,
  Masthead,
  MastheadMain,
  MastheadBrand,
  PageSection,
  Card,
  CardTitle,
  CardBody,
  Grid,
  GridItem,
  Button,
  Badge,
  Label,
  Flex,
  FlexItem,
  Title,
} from '@patternfly/react-core';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ReferenceLine,
} from 'recharts';

interface UserControl {
  user: string;
  team: string;
  state: 'running' | 'stopped';
  agent: 'triage' | 'research' | 'assistant';
  mode: 'naive' | 'paced' | 'runaway';
  limit_tpm: number;
}

interface UserMetrics {
  tpm: number;
  status_429_count: number;
  total_calls: number;
  latest_status: number;
  latest_node: string;
  latency_ms: number;
}

interface TelemetryEvent {
  seq: number;
  ts: string;
  user: string;
  team: string;
  agent: string;
  mode: string;
  node: string;
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  attempt: number;
}

interface AgentInfo {
  user: string;
  team: string;
  display_name: string;
  task_description: string;
  agent: string;
  mode: string;
}

interface DashboardState {
  timestamp: number;
  control: Record<string, UserControl>;
  limits: Record<string, number>;
  agent_registry: Record<string, AgentInfo>;
  user_metrics: Record<string, UserMetrics>;
  chart_series: Array<Record<string, any>>;
  recent_events: TelemetryEvent[];
}

const LANE_COLORS = ['#0066CC', '#3E8635', '#6F38B1', '#F4C145', '#EC7A08'];

const LimitEditor: React.FC<{
  userId: string;
  currentLimit: number;
  onSave: (userId: string, updates: Partial<UserControl>) => void;
}> = ({ userId, currentLimit, onSave }) => {
  const [draft, setDraft] = useState(String(currentLimit));
  const [focused, setFocused] = useState(false);
  const step = 50;

  // Only sync from SSE when the field is not focused
  useEffect(() => {
    if (!focused) setDraft(String(currentLimit));
  }, [currentLimit, focused]);

  const commit = (val: number) => {
    if (!isNaN(val) && val >= 50) onSave(userId, { limit_tpm: val });
  };
  const decrement = () => commit(Math.max(50, currentLimit - step));
  const increment = () => commit(currentLimit + step);
  const onBlur = () => { setFocused(false); commit(parseInt(draft, 10)); };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
    if (e.key === 'ArrowUp') { e.preventDefault(); commit(currentLimit + step); }
    if (e.key === 'ArrowDown') { e.preventDefault(); commit(Math.max(50, currentLimit - step)); }
  };

  const btnStyle: React.CSSProperties = {
    width: '28px', height: '28px', fontSize: '1.1rem', lineHeight: 1,
    cursor: 'pointer', border: '1px solid #D2D2D2', borderRadius: '3px',
    background: '#F5F5F5', display: 'flex', alignItems: 'center',
    justifyContent: 'center', flexShrink: 0,
  };

  return (
    <div style={{ fontSize: '0.9rem', marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '6px' }}>
      <strong>TPM limit:</strong>
      <button style={btnStyle} onClick={decrement} title="Decrease by 50">−</button>
      <input
        type="number"
        min={50}
        step={step}
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={onBlur}
        onKeyDown={onKey}
        style={{
          width: '70px', textAlign: 'center', fontSize: '0.9rem',
          padding: '2px 4px', border: '1px solid #D2D2D2', borderRadius: '3px',
          fontWeight: 'bold',
        }}
      />
      <button style={btnStyle} onClick={increment} title="Increase by 50">+</button>
    </div>
  );
};

export const App: React.FC = () => {
  const [data, setData] = useState<DashboardState>({
    timestamp: Date.now() / 1000,
    control: {},
    limits: {},
    agent_registry: {},
    user_metrics: {},
    chart_series: [],
    recent_events: [],
  });

  useEffect(() => {
    const sse = new EventSource('/events/stream');
    sse.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setData(parsed);
      } catch (err) {
        console.error('SSE parse error:', err);
      }
    };
    return () => sse.close();
  }, []);

  const triggerAct = async (actNum: number) => {
    try {
      await fetch(`/act/${actNum}`, { method: 'POST' });
    } catch (e) {
      console.error(e);
    }
  };

  const triggerReset = async () => {
    try {
      await fetch('/reset', { method: 'POST' });
    } catch (e) {
      console.error(e);
    }
  };

  const updateControl = async (user: string, updates: Partial<UserControl>) => {
    try {
      await fetch(`/control/${user}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      });
    } catch (e) {
      console.error(e);
    }
  };

  const renderUserLane = (userId: string, color: string) => {
    const info = data.agent_registry[userId];
    const title = info?.display_name || userId;
    const teamLabel = info?.team ? `Team: ${info.team}` : '';
    const taskLabel = info?.task_description || (
      info?.agent === 'triage' ? 'Processing dispute letters' :
      info?.agent === 'assistant' ? 'Customer Q&A (multi-turn)' :
      'Policy research (fan-out)'
    );
    const ctrl = data.control[userId] || {
      user: userId,
      state: 'stopped',
      agent: 'triage',
      mode: 'naive',
      limit_tpm: 2000,
    };
    const metrics = data.user_metrics[userId] || {
      tpm: 0,
      status_429_count: 0,
      total_calls: 0,
      latest_status: 200,
      latest_node: 'idle',
      latency_ms: 0,
    };

    const isThrottled = metrics.status_429_count > 0 || metrics.latest_status === 429;
    const isRunning = ctrl.state === 'running';
    const isOnline = metrics.total_calls > 0 || isRunning;

    return (
      <Card
        style={{
          borderLeft: `6px solid ${isThrottled ? '#C9190B' : isRunning ? color : '#D2D2D2'}`,
          backgroundColor: isThrottled ? '#FFF5F5' : '#FFFFFF',
          boxShadow: isThrottled ? '0 0 10px rgba(201, 25, 11, 0.25)' : undefined,
        }}
      >
        <CardTitle>
          <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }}>
            <FlexItem>
              <Flex alignItems={{ default: 'alignItemsCenter' }} gap={{ default: 'gapSm' }}>
                <FlexItem>
                  <span style={{
                    display: 'inline-block',
                    width: '10px',
                    height: '10px',
                    borderRadius: '50%',
                    backgroundColor: isOnline ? '#3E8635' : '#6A6E73',
                    flexShrink: 0,
                  }} title={isOnline ? 'Agent connected' : 'Agent not seen'} />
                </FlexItem>
                <FlexItem>
                  <Title headingLevel="h3" size="lg">
                    {title}
                  </Title>
                </FlexItem>
              </Flex>
              <div style={{ fontSize: '0.85rem', color: '#6A6E73' }}>
                {teamLabel}
              </div>
            </FlexItem>
            <FlexItem>
              {isThrottled ? (
                <Label color="red">THROTTLED</Label>
              ) : isRunning ? (
                <Label color="green">RUNNING</Label>
              ) : (
                <Label color="grey">IDLE</Label>
              )}
            </FlexItem>
          </Flex>
        </CardTitle>
        <CardBody>
          <Grid hasGutter>
            <GridItem span={4}>
              <div style={{ fontSize: '0.9rem', marginBottom: '6px' }}>
                <strong>Task:</strong> {taskLabel}
              </div>
              <div style={{ fontSize: '0.9rem', marginBottom: '6px' }}>
                <strong>Behaviour:</strong>{' '}
                <Badge isRead={ctrl.mode !== 'runaway'}>
                  {ctrl.mode === 'runaway' ? 'Unrestricted' : ctrl.mode === 'paced' ? 'Rate-aware' : 'Naive retry'}
                </Badge>
              </div>
              <div style={{ fontSize: '0.9rem', marginBottom: '6px' }}>
                <strong>Current step:</strong> <code>{metrics.latest_node}</code>
              </div>
              <div style={{ fontSize: '0.9rem', marginBottom: '6px' }}>
                <strong>Tokens/min (1m):</strong>{' '}
                <span style={{ color: isThrottled ? '#C9190B' : isRunning ? '#3E8635' : 'inherit', fontWeight: 'bold' }}>
                  {metrics.tpm}
                </span>
              </div>
              <div style={{ fontSize: '0.9rem', marginBottom: '6px' }}>
                <strong>Throttled requests (1m):</strong>{' '}
                <span style={{ color: metrics.status_429_count > 0 ? '#C9190B' : 'inherit', fontWeight: 'bold' }}>
                  {metrics.status_429_count}
                </span>
              </div>
              <LimitEditor userId={userId} currentLimit={ctrl.limit_tpm} onSave={updateControl} />
              <Flex gap={{ default: 'gapSm' }} style={{ marginTop: '12px' }}>
                {isRunning ? (
                  <Button variant="danger" size="sm" onClick={() => updateControl(userId, { state: 'stopped' })}>
                    Stop
                  </Button>
                ) : (
                  <Button variant="primary" size="sm" onClick={() => updateControl(userId, { state: 'running' })}>
                    Start
                  </Button>
                )}
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() =>
                    updateControl(userId, {
                      mode: ctrl.mode === 'paced' ? 'runaway' : ctrl.mode === 'runaway' ? 'naive' : 'paced',
                    })
                  }
                >
                  {ctrl.mode === 'runaway' ? '⚡ Unrestricted' : ctrl.mode === 'paced' ? '✓ Rate-aware' : '↺ Naive retry'}
                </Button>
              </Flex>
            </GridItem>
            <GridItem span={8}>
              <div style={{ height: 160, width: '100%' }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={data.chart_series}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="time" tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 10 }} domain={[0, 'auto']} />
                    <Tooltip />
                    <ReferenceLine
                      y={ctrl.limit_tpm}
                      stroke="#C9190B"
                      strokeDasharray="4 4"
                      label={{ value: 'Limit', fill: '#C9190B', fontSize: 10 }}
                    />
                    <Line
                      type="monotone"
                      dataKey={`${userId}_tokens`}
                      name="Tokens (5s)"
                      stroke={color}
                      strokeWidth={2}
                      dot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </GridItem>
          </Grid>
        </CardBody>
      </Card>
    );
  };

  return (
    <Page>
      <Masthead style={{ backgroundColor: '#151515', color: '#FFF' }}>
        <MastheadMain>
          <MastheadBrand>
            <Title headingLevel="h1" size="xl" style={{ color: '#FFF' }}>
              🐒 MaaS Agent Testing Harness
            </Title>
          </MastheadBrand>
        </MastheadMain>
      </Masthead>

      <PageSection variant="secondary">
        <Card isPlain>
          <CardBody>
            <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }} alignItems={{ default: 'alignItemsCenter' }}>
              <FlexItem>
                <Title headingLevel="h2" size="md">
                  Simulation Controls
                </Title>
              </FlexItem>
              <FlexItem>
                <Flex gap={{ default: 'gapMd' }}>
                  <Button variant="primary" onClick={() => triggerAct(1)}>
                    Normal Operations
                  </Button>
                  <Button variant="warning" onClick={() => triggerAct(2)}>
                    Runaway Agent
                  </Button>
                  <Button variant="danger" onClick={triggerReset}>
                    Reset
                  </Button>
                </Flex>
              </FlexItem>
            </Flex>
          </CardBody>
        </Card>
      </PageSection>

      <PageSection>
        <Grid hasGutter>
          {/* Main User Lanes */}
          <GridItem span={12}>
            {Object.keys(data.control).length === 0 ? (
              <Card>
                <CardBody>
                  <div style={{ padding: '40px', textAlign: 'center', color: '#6A6E73' }}>
                    <Title headingLevel="h3" size="lg">No agents connected</Title>
                  </div>
                </CardBody>
              </Card>
            ) : (
              <Flex direction={{ default: 'column' }} gap={{ default: 'gapMd' }}>
                {Object.keys(data.control).map((userId, idx) =>
                  renderUserLane(userId, LANE_COLORS[idx % LANE_COLORS.length])
                )}
              </Flex>
            )}
          </GridItem>

          {/* Bottom Panel: Live Event Log */}
          <GridItem span={12}>
            <Card>
              <CardTitle>Logs (Last 30 Model Invocations)</CardTitle>
              <CardBody>
                <div style={{ maxHeight: '220px', overflowY: 'auto', fontSize: '0.8rem', fontFamily: 'monospace' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #D2D2D2', textAlign: 'left', color: '#6A6E73' }}>
                        <th style={{ padding: '6px' }}>Timestamp</th>
                        <th style={{ padding: '6px' }}>Agent</th>
                        <th style={{ padding: '6px' }}>Task / Behaviour</th>
                        <th style={{ padding: '6px' }}>Step</th>
                        <th style={{ padding: '6px' }}>Status</th>
                        <th style={{ padding: '6px' }}>Prompt / Output Tokens</th>
                        <th style={{ padding: '6px' }}>Latency</th>
                        <th style={{ padding: '6px' }}>Attempt</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.recent_events && data.recent_events.length > 0 ? (
                        [...data.recent_events].reverse().map((ev) => (
                          <tr
                            key={ev.seq}
                            style={{
                              borderBottom: '1px solid #F0F0F0',
                              backgroundColor: ev.status === 429 ? '#FFF0F0' : 'transparent',
                            }}
                          >
                            <td style={{ padding: '6px' }}>{ev.ts ? ev.ts.slice(11, 19) : ''}</td>
                            <td style={{ padding: '6px' }}>
                              <strong>
                                {data.agent_registry[ev.user]?.display_name || ev.user}
                              </strong>
                            </td>
                            <td style={{ padding: '6px' }}>
                              {ev.agent === 'triage' ? 'Processing dispute letters' : ev.agent === 'assistant' ? 'Customer Q&A' : 'Policy research'}{' '}
                              ({ev.mode === 'runaway' ? 'Unrestricted' : ev.mode === 'paced' ? 'Rate-aware' : 'Naive retry'})
                            </td>
                            <td style={{ padding: '6px' }}>{ev.node}</td>
                            <td style={{ padding: '6px' }}>
                              <Badge isRead={ev.status === 200} style={{ backgroundColor: ev.status === 429 ? '#C9190B' : undefined }}>
                                {ev.status}
                              </Badge>
                            </td>
                            <td style={{ padding: '6px' }}>
                              {ev.prompt_tokens} in / {ev.completion_tokens} out
                            </td>
                            <td style={{ padding: '6px' }}>{ev.latency_ms} ms</td>
                            <td style={{ padding: '6px' }}>#{ev.attempt}</td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td colSpan={8} style={{ padding: '12px', textAlign: 'center', color: '#6A6E73' }}>
                              —
                            </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </CardBody>
            </Card>
          </GridItem>
        </Grid>
      </PageSection>
    </Page>
  );
};

export default App;
