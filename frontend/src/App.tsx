import { useEffect, useRef, useState, type CSSProperties } from 'react';
import {
  ArrowDownToLine, BookOpen, Box, Braces, Check, ChevronDown, ChevronRight, CircleCheck, Code2, Copy,
  Cpu, FileCode2, FolderOpen, GitBranch, KeyRound, Loader2, LockKeyhole, Pencil,
  Plus, Puzzle, RotateCcw, Settings2, ShieldCheck, Terminal, Trash2, Upload, X,
} from 'lucide-react';
import type { BuildTestSteps, FixedCommand, Manifest, Mount, NamedCommand, Pricing, Skill } from './manifest.generated';

type Capability = {
  id: string;
  label: string;
  description: string;
  tools: string[];
  configuration: string | null;
  broad_access: boolean;
};
type Catalog = {
  version: number;
  app_version: string;
  bash_programs: string[];
  capabilities: Capability[];
  default_manifest: Manifest;
  presets: { label: string; manifest: Manifest }[];
};
type CliBinary = {
  os: string;
  arch: string;
  version: string;
  filename: string;
  sha256: string;
};
type RuntimeTool = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  kind: 'native' | 'action' | 'mcp';
  argv?: string[];
  cwd?: string;
  timeoutSeconds?: number;
  argInput?: { name: string; flag: string; schema: Record<string, unknown> };
  server?: string;
  remoteName?: string;
};
type RuntimeConfig = {
  version: number;
  name: string;
  provider: { kind: string; baseUrl: string | null; apiKeyEnv: string | null };
  model: string;
  maxTurns: number;
  wallClockLimitSeconds: number;
  pricing: Pricing | null;
  systemPrompt: string;
  mounts: { target: string; hostPath: string; readOnly: boolean }[];
  tools: RuntimeTool[];
  mcpServers: Record<string, unknown>[];
};
type Notice = { type: 'error' | 'success'; text: string } | null;
type PreviewTab = 'prompt' | 'tools' | 'config';

const ICONS: Record<string, typeof Code2> = {
  repo_read: FileCode2,
  file_edit: Pencil,
  run_tests: Terminal,
  git_read: GitBranch,
  git_write: GitBranch,
  mcp_tools: Puzzle,
};

async function responseJson(response: Response) {
  let data: any;
  try {
    data = await response.json();
  } catch {
    throw new Error('The builder API is unavailable. Start the Python server on port 8000.');
  }
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(Array.isArray(detail)
      ? detail.map((item: { loc: string[]; msg: string }) =>
        `${item.loc.filter(part => part !== 'body').join(' › ') || 'Manifest'}: ${item.msg.replace(/^Value error, /, '')}`).join('\n')
      : detail || 'Request failed.');
  }
  return data;
}

function saveFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function detectOS(): 'macos' | 'windows' | 'linux' | null {
  const platforms = [
    (navigator as any).userAgentData?.platform,
    navigator.platform,
    navigator.userAgent,
  ];
  for (const platform of platforms) {
    if (typeof platform !== 'string') continue;
    if (/mac/i.test(platform)) return 'macos';
    if (/win/i.test(platform)) return 'windows';
    if (/linux/i.test(platform)) return 'linux';
  }
  return null;
}

function CodePreview({ text }: { text: string }) {
  return <div className="code-preview">{text.split('\n').map((line, index) =>
    <div className="code-line" key={`${index}-${line}`}>
      <span className="line-number">{index + 1}</span>
      <span className={line.startsWith('#') ? 'heading-line' : line.startsWith('- ') ? 'list-line' : ''}>{line || ' '}</span>
    </div>)}</div>;
}

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [cliBinaries, setCliBinaries] = useState<CliBinary[]>([]);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [preview, setPreview] = useState<RuntimeConfig | null>(null);
  const [previewKey, setPreviewKey] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [view, setView] = useState<'form' | 'json'>('form');
  const [previewTab, setPreviewTab] = useState<PreviewTab>('prompt');
  const [jsonDraft, setJsonDraft] = useState('');
  const [downloaded, setDownloaded] = useState(false);
  const [showHandoff, setShowHandoff] = useState(false);
  const [capabilityMenuOpen, setCapabilityMenuOpen] = useState(false);
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(() => new Set());
  const [stickyOffset, setStickyOffset] = useState(0);
  const importInput = useRef<HTMLInputElement>(null);
  const snapshotInput = useRef<HTMLInputElement>(null);
  const workspaceSticky = useRef<HTMLDivElement>(null);
  const modalClose = useRef<HTMLButtonElement>(null);
  const capabilityPicker = useRef<HTMLDivElement>(null);
  const capabilityTrigger = useRef<HTMLButtonElement>(null);
  const key = manifest ? JSON.stringify(manifest) : '';
  const current = Boolean(preview && previewKey === key && !error);
  const ready = Boolean(manifest && catalog);

  const toggleSection = (id: string) => setCollapsedSections(currentSections => {
    const nextSections = new Set(currentSections);
    if (nextSections.has(id)) nextSections.delete(id);
    else nextSections.add(id);
    return nextSections;
  });

  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/capabilities', { signal: controller.signal })
      .then(responseJson)
      .then((data: Catalog) => {
        setCatalog(data);
        setManifest(data.default_manifest);
      })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/cli-binaries', { signal: controller.signal })
      .then(responseJson)
      .then((data: { binaries: CliBinary[] }) => setCliBinaries(data.binaries))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setError('');
      fetch('/api/compile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: key,
        signal: controller.signal,
      })
        .then(responseJson)
        .then((data: RuntimeConfig) => {
          if (!controller.signal.aborted) {
            setPreview(data);
            setPreviewKey(key);
          }
        })
        .catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [key]);

  useEffect(() => {
    if (!manifest || manifest.prompt.override !== null || !preview || previewKey !== key || error) return;
    setManifest(previous => {
      if (!previous || JSON.stringify(previous) !== key || previous.prompt.override !== null) return previous;
      return { ...previous, prompt: { ...previous.prompt, override: preview.systemPrompt } };
    });
    setDownloaded(false);
  }, [error, key, manifest, preview, previewKey]);

  useEffect(() => {
    const element = workspaceSticky.current;
    if (!ready || !element) return;
    const updateOffset = () => setStickyOffset(element.getBoundingClientRect().height);
    updateOffset();
    const observer = new ResizeObserver(updateOffset);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ready]);

  useEffect(() => {
    if (!showHandoff) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setShowHandoff(false);
    };
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', closeOnEscape);
    modalClose.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', closeOnEscape);
      previousFocus?.focus();
    };
  }, [showHandoff]);

  useEffect(() => {
    if (!capabilityMenuOpen) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!capabilityPicker.current?.contains(event.target as Node)) setCapabilityMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setCapabilityMenuOpen(false);
        capabilityTrigger.current?.focus();
      }
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [capabilityMenuOpen]);

  function normalizeManifest(value: unknown): Manifest {
    if (!catalog || typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new Error('A manifest must be a JSON object.');
    }
    const input = value as Partial<Manifest>;
    const provider = input.provider || catalog.default_manifest.provider;
    const ownsApiKey = Object.prototype.hasOwnProperty.call(input, 'api_key_env');
    return {
      ...structuredClone(catalog.default_manifest),
      ...input,
      provider,
      base_url: Object.prototype.hasOwnProperty.call(input, 'base_url') ? input.base_url! : null,
      api_key_env: ownsApiKey ? input.api_key_env! : provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY',
      capabilities: input.capabilities || structuredClone(catalog.default_manifest.capabilities),
      mounts: input.mounts || structuredClone(catalog.default_manifest.mounts),
      mcp_servers: input.mcp_servers || [],
      skills: input.skills || [],
      prompt: { ...catalog.default_manifest.prompt, ...(input.prompt || {}) },
    };
  }

  async function applyManifest(value: unknown) {
    const candidate = normalizeManifest(value);
    const compiled: RuntimeConfig = await responseJson(await fetch('/api/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(candidate),
    }));
    setManifest(candidate);
    setPreview(compiled);
    setPreviewKey(JSON.stringify(candidate));
    setError('');
    setDownloaded(false);
    setView('form');
    setNotice({ type: 'success', text: 'Manifest loaded, validated, and compiled.' });
  }

  async function importManifest(file?: File) {
    if (!file) return;
    try {
      if (file.size > 500000) throw new Error('Manifest files must be under 500 KB.');
      await applyManifest(JSON.parse(await file.text()));
    } catch (reason) {
      setNotice({ type: 'error', text: reason instanceof Error ? reason.message : 'Invalid manifest.' });
    }
  }

  async function importSnapshot(file?: File) {
    if (!file || !manifest) return;
    try {
      if (file.size > 500000) throw new Error('Snapshots must be under 500 KB.');
      const server = JSON.parse(await file.text());
      if (!Array.isArray(server.snapshot)) throw new Error('Import JSON produced by python -m builder.snapshot.');
      await applyManifest({
        ...manifest,
        capabilities: [...new Set([...manifest.capabilities, 'mcp_tools'])],
        mcp_servers: [...manifest.mcp_servers.filter(item => item.name !== server.name), server],
      });
      setNotice({ type: 'success', text: 'MCP snapshot imported. Select the remote tools this agent may use.' });
    } catch (reason) {
      setNotice({ type: 'error', text: reason instanceof Error ? reason.message : 'Invalid snapshot.' });
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setNotice({ type: 'success', text: 'Copied to clipboard.' });
    } catch {
      setNotice({ type: 'error', text: 'Clipboard unavailable. Select and copy the text manually.' });
    }
  }

  function downloadManifest() {
    if (!manifest || !current) return;
    saveFile(new Blob([JSON.stringify(manifest, null, 2) + '\n'], { type: 'application/json' }), `${manifest.name}.json`);
    setDownloaded(true);
    setNotice({ type: 'success', text: 'Manifest downloaded. Run it locally with the standalone CLI commands shown below.' });
  }

  if (!manifest || !catalog) {
    return <div className="loading-screen"><div className="brand-mark">H</div><h1>Harness Pro</h1>
      {error ? <p role="alert">{error}</p> : <p><Loader2 className="spin" size={16} /> Loading the standalone capability catalog…</p>}
    </div>;
  }

  const m = manifest;
  const update = (patch: Partial<Manifest>) => {
    setDownloaded(false);
    setManifest(previous => previous ? { ...previous, ...patch } : previous);
  };
  const updateMount = (index: number, patch: Partial<Mount>) => update({
    mounts: m.mounts.map((mount, itemIndex) => itemIndex === index ? { ...mount, ...patch } : mount),
  });
  const updateSkill = (index: number, patch: Partial<Skill>) => update({
    skills: m.skills.map((skill, itemIndex) => itemIndex === index ? { ...skill, ...patch } : skill),
  });
  const updateCommandItem = (index: number, patch: Partial<NamedCommand>) => update({
    commands: m.commands.map((command, itemIndex) => itemIndex === index ? { ...command, ...patch } : command),
  });
  const updateBuildTestStep = (key: keyof BuildTestSteps, patch: Partial<FixedCommand> | null) => update({
    build_test_steps: {
      format: null,
      build: null,
      test: null,
      ...m.build_test_steps,
      [key]: patch === null ? null : {
        argv: ['echo', 'ok'],
        working_directory: m.mounts[0]?.target || 'repo',
        timeout_seconds: 120,
        ...(m.build_test_steps?.[key] || {}),
        ...patch,
      },
    },
  });
  const updateCommand = (patch: Partial<FixedCommand>) => update({
    test_command: {
      argv: ['npm', 'test'],
      working_directory: m.mounts[0]?.target || 'repo',
      timeout_seconds: 120,
      ...m.test_command,
      ...patch,
    },
  });
  const toggleCapability = (capability: Capability) => {
    const selected = m.capabilities.includes(capability.id);
    const patch: Partial<Manifest> = {
      capabilities: selected
        ? m.capabilities.filter(id => id !== capability.id)
        : [...m.capabilities, capability.id],
    };
    if (!selected && capability.configuration === 'test_command' && !m.test_command) {
      patch.test_command = { argv: ['npm', 'test'], working_directory: m.mounts[0]?.target || 'repo', timeout_seconds: 120 };
    }
    if (!selected && capability.configuration === 'git_directory' && !m.git_directory) {
      patch.git_directory = m.mounts[0]?.target || 'repo';
    }
    if (!selected && capability.configuration === 'allowed_bash_programs' && !m.allowed_bash_programs.length) {
      patch.allowed_bash_programs = ['cat', 'grep', 'find', 'ls'];
    }
    if (!selected && capability.configuration === 'commands' && !m.commands.length) {
      patch.commands = [{ name: 'my-command', description: 'Describe what this command does.', argv: ['echo', 'hello'], working_directory: m.mounts[0]?.target || 'repo', timeout_seconds: 120 }];
    }
    if (!selected && capability.configuration === 'build_test_steps' && !m.build_test_steps) {
      patch.build_test_steps = { format: null, build: null, test: null };
    }
    update(patch);
  };
  const updatePricing = (field: keyof Pricing, value: string) => {
    if (!m.pricing) return;
    update({ pricing: { ...m.pricing, [field]: value === '' ? null : Number(value) } });
  };
  const resetPromptToGenerated = async () => {
    try {
      const generatedManifest = { ...m, prompt: { ...m.prompt, override: null } };
      const compiled = await responseJson(await fetch('/api/compile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(generatedManifest),
      })) as RuntimeConfig;
      update({ prompt: { ...m.prompt, override: compiled.systemPrompt } });
      setPreviewTab('prompt');
    } catch (reason) {
      setNotice({ type: 'error', text: reason instanceof Error ? reason.message : 'Unable to compile the generated prompt.' });
    }
  };
  const previewText = previewTab === 'prompt'
    ? preview?.systemPrompt || ''
    : previewTab === 'tools'
      ? JSON.stringify(preview?.tools || [], null, 2)
      : JSON.stringify(preview || {}, null, 2);
  const manifestName = `${m.name}.json`;
  const detectedOS = detectOS();
  const matchedBinary = cliBinaries.find(binary => binary.os === detectedOS) ?? null;
  const cliCommand = matchedBinary ? `./${matchedBinary.filename}` : 'harness';
  const selectedCapabilities = catalog.capabilities.filter(capability => m.capabilities.includes(capability.id));
  const capabilitySelectionLabel = selectedCapabilities.length === 0
    ? 'Select capabilities'
    : selectedCapabilities.length <= 2
      ? selectedCapabilities.map(capability => capability.label).join(', ')
      : `${selectedCapabilities.slice(0, 2).map(capability => capability.label).join(', ')} +${selectedCapabilities.length - 2}`;
  const commands = [
    `${cliCommand} compile --manifest ${manifestName} -o ${m.name}.runtime.json`,
    `${cliCommand} run --manifest ${manifestName} --prompt "Describe the task" --trace-file ${m.name}.trace.jsonl`,
  ];

  return <>
    <header className="site-header">
      <a className="brand" href="/" aria-label="Harness Pro home"><span className="brand-mark">H</span>harness<span className="pro-tag">PRO</span><span className="pro-tag">v{catalog.app_version}</span></a>
      <div className="header-right"><span className="header-runtime"><CircleCheck size={15} /> Standalone runtime</span></div>
    </header>
    <main style={{ '--workspace-sticky-height': `${stickyOffset}px` } as CSSProperties}>
      <section className="hero">
        <div><div className="eyebrow"><span className="status-dot" /> ONE WORKFLOW. ONE AGENT.</div>
          <h1>Build an agent.<br /><span>Run it anywhere.</span></h1>
          <p>Choose a provider, constrain the tools and workspace, then download a validated manifest.<br className="desktop-break" /> Compile or run it locally with the standalone Harness CLI.</p>
        </div>
        <div className="hero-aside"><div className="stack-diagram" aria-hidden="true"><div /><div /><div><Box size={27} /></div><span className="diagram-dot" /></div>
          <span>YOUR WORKFLOW, COMPILED.</span><p>One manifest. One bounded runtime.</p>
        </div>
      </section>

      <div className="workspace-sticky" ref={workspaceSticky}>
        <div className="workspace-toolbar">
          <div className="presets"><span>Start from</span>{catalog.presets.map(preset =>
            <button key={preset.label} className={m.name === preset.manifest.name ? 'preset active' : 'preset'} onClick={() => {
              setManifest(structuredClone(preset.manifest)); setDownloaded(false); setView('form'); setNotice(null);
            }}>{preset.label === 'Reviewer' ? <FileCode2 size={14} /> : <Terminal size={14} />}{preset.label}</button>)}</div>
          <div className="manifest-actions">
            <button className="text-button" onClick={() => setShowHandoff(true)}><BookOpen size={14} /> How it works</button>
            <input ref={importInput} type="file" accept=".json,application/json" hidden onChange={event => { void importManifest(event.target.files?.[0]); event.target.value = ''; }} />
            <button className="text-button" onClick={() => importInput.current?.click()}><Upload size={14} /> Import manifest</button>
            <button className="text-button" disabled={!current} onClick={downloadManifest}><ArrowDownToLine size={14} /> Download JSON</button>
          </div>
        </div>
        {notice && <div className={`notice ${notice.type}`} role={notice.type === 'error' ? 'alert' : 'status'}>
          <span>{notice.text}</span><button aria-label="Dismiss notification" onClick={() => setNotice(null)}><X size={16} /></button>
        </div>}
      </div>

      <div className="builder-layout">
        <div className="configuration">
          <div className="config-tabs" role="tablist" aria-label="Manifest editor">
            <button role="tab" aria-selected={view === 'form'} className={view === 'form' ? 'active' : ''} onClick={() => setView('form')}><Settings2 size={15} /> Configuration</button>
            <button role="tab" aria-selected={view === 'json'} className={view === 'json' ? 'active' : ''} onClick={() => { setJsonDraft(JSON.stringify(m, null, 2)); setView('json'); }}><Braces size={15} /> Manifest JSON</button>
            <span>manifest v1</span>
          </div>

          {view === 'json' ? <section className="form-section json-section">
            <div className="section-title"><h2>Edit the source of truth</h2><Braces size={20} /></div>
            <p className="section-description">Paste or hand-edit a manifest, then validate it through the same standalone compiler used by the CLI.</p>
            <textarea className="json-editor" aria-label="Manifest JSON" spellCheck={false} value={jsonDraft} onChange={event => setJsonDraft(event.target.value)} />
            <button className="primary-button" onClick={async () => {
              try { await applyManifest(JSON.parse(jsonDraft)); }
              catch (reason) { setNotice({ type: 'error', text: reason instanceof Error ? reason.message : 'Invalid JSON.' }); }
            }}>Validate and apply <ChevronRight size={16} /></button>
          </section> : <>
            <section className="form-section identity-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('purpose')} onClick={() => toggleSection('purpose')}><div><span className="section-number">01</span><h2>Give it a purpose</h2></div><span className="section-title-trailing"><span className="section-hint">THE BASICS</span><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('purpose') && <>
              <div className="field-grid">
                <label>Agent name<input value={m.display_name} onChange={event => update({ display_name: event.target.value })} placeholder="Reviewer" /></label>
                <label>Manifest ID<input className="mono" value={m.name} onChange={event => update({ name: event.target.value })} placeholder="reviewer" /></label>
              </div>
              <label>Description<input value={m.description} onChange={event => update({ description: event.target.value })} placeholder="What should this agent do?" /></label>
              </>}
            </section>

            <section className="form-section provider-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('provider')} onClick={() => toggleSection('provider')}><div><span className="section-number">02</span><h2>Choose its provider</h2></div><span className="section-title-trailing"><KeyRound size={19} /><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('provider') && <>
              <p className="section-description">The manifest stores an environment-variable name, never a secret value.</p>
              <div className="field-grid">
                <label>Provider<select value={m.provider} onChange={event => {
                  const provider = event.target.value as Manifest['provider'];
                  update(provider === 'anthropic'
                    ? { provider, base_url: null, api_key_env: 'ANTHROPIC_API_KEY', model: 'claude-sonnet-4-5-20250929' }
                    : { provider, base_url: 'http://localhost:11434/v1', api_key_env: null, model: 'qwen2.5-coder:7b' });
                }}><option value="anthropic">Anthropic</option><option value="openai-compatible">OpenAI-compatible</option></select></label>
                <label>Model<input className="mono" value={m.model} onChange={event => update({ model: event.target.value })} /></label>
              </div>
              {m.provider === 'openai-compatible' && <label>Base URL<input className="mono" value={m.base_url || ''} onChange={event => update({ base_url: event.target.value })} placeholder="https://provider.example/v1" /></label>}
              <label>API key environment variable <span className="label-note">leave blank only when the endpoint needs no key</span>
                <input className="mono" value={m.api_key_env || ''} onChange={event => update({ api_key_env: event.target.value || null })} placeholder={m.provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY'} />
              </label>
              <div className="field-grid limits-grid">
                <label>Turn limit<input type="number" min={1} max={100} value={m.turn_limit} onChange={event => update({ turn_limit: Number(event.target.value) })} /></label>
                <label>Wall-clock limit (seconds)<input type="number" min={1} max={86400} value={m.wall_clock_limit_seconds} onChange={event => update({ wall_clock_limit_seconds: Number(event.target.value) })} /></label>
              </div>
              </>}
            </section>

            <section className="form-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('pricing')} onClick={() => toggleSection('pricing')}><div><span className="section-number">03</span><h2>Estimate usage cost</h2></div><span className="section-title-trailing"><span className="section-hint">OPTIONAL</span><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('pricing') && <>
              <p className="section-description">Add provider rates to estimate token costs in runtime traces.</p>
              <label className="toggle-control">
                <span><strong>Estimate cost</strong><small>Rates are in US dollars per million tokens.</small></span>
                <input type="checkbox" role="switch" checked={m.pricing !== null} onChange={() => update({
                  pricing: m.pricing === null ? {
                    input_per_million: null,
                    output_per_million: null,
                    cache_read_per_million: null,
                    cache_write_per_million: null,
                  } : null,
                })} />
              </label>
              {m.pricing && <div className="field-grid pricing-fields">
                <label>Input $/M tokens<input type="number" min={0} step="any" value={m.pricing.input_per_million ?? ''} onChange={event => updatePricing('input_per_million', event.target.value)} /></label>
                <label>Output $/M tokens<input type="number" min={0} step="any" value={m.pricing.output_per_million ?? ''} onChange={event => updatePricing('output_per_million', event.target.value)} /></label>
                <label>Cache read $/M tokens<input type="number" min={0} step="any" value={m.pricing.cache_read_per_million ?? ''} onChange={event => updatePricing('cache_read_per_million', event.target.value)} /></label>
                <label>Cache write $/M tokens<input type="number" min={0} step="any" value={m.pricing.cache_write_per_million ?? ''} onChange={event => updatePricing('cache_write_per_million', event.target.value)} /></label>
              </div>}
              </>}
            </section>

            <section className="form-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('capabilities')} onClick={() => toggleSection('capabilities')}><div><span className="section-number">04</span><h2>Choose its capabilities</h2></div><span className="section-title-trailing"><span className="count-badge">{m.capabilities.length} selected</span><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('capabilities') && <>
              <p className="section-description">Only these bounded tools enter the compiled runtime configuration.</p>
              <div className={`capability-picker ${capabilityMenuOpen ? 'open' : ''}`} ref={capabilityPicker}>
                <button
                  ref={capabilityTrigger}
                  type="button"
                  className="capability-trigger"
                  aria-haspopup="listbox"
                  aria-expanded={capabilityMenuOpen}
                  aria-controls="capability-options"
                  onClick={() => setCapabilityMenuOpen(open => !open)}
                >
                  <span className={selectedCapabilities.length ? '' : 'placeholder'}>{capabilitySelectionLabel}</span>
                  <ChevronDown size={17} aria-hidden="true" />
                </button>
                {capabilityMenuOpen && <div id="capability-options" className="capability-options" role="listbox" aria-label="Capabilities" aria-multiselectable="true">
                  {catalog.capabilities.map(capability => {
                    const Icon = ICONS[capability.id] || Puzzle;
                    const selected = m.capabilities.includes(capability.id);
                    return <button
                      key={capability.id}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      className={`capability-option ${selected ? 'selected' : ''}`}
                      onClick={() => toggleCapability(capability)}
                    >
                      <span className="capability-option-icon"><Icon size={18} strokeWidth={1.7} /></span>
                      <span className="capability-option-copy">
                        <strong>{capability.label}</strong>
                        <small>{capability.description}</small>
                      </span>
                      <span className="checkbox" aria-hidden="true">{selected && <Check size={12} strokeWidth={3} />}</span>
                    </button>;
                  })}
                </div>}
              </div>
              {m.capabilities.includes('run_tests') && m.test_command && <div className="subform">
                <div className="subform-heading"><Terminal size={16} /><h3>Fixed test / build command</h3></div>
                <div className="field-grid">
                  <label>Executable<input className="mono" value={m.test_command.argv[0] || ''} onChange={event => updateCommand({ argv: [event.target.value, ...m.test_command!.argv.slice(1)] })} /></label>
                  <label>Working directory<select value={m.test_command.working_directory} onChange={event => updateCommand({ working_directory: event.target.value })}>{m.mounts.map(mount => <option key={mount.target}>{mount.target}</option>)}</select></label>
                </div>
                <label>Arguments <span className="label-note">one per line; passed literally</span><textarea className="mono short-textarea" value={m.test_command.argv.slice(1).join('\n')} onChange={event => updateCommand({ argv: [m.test_command!.argv[0], ...(event.target.value ? event.target.value.split('\n') : [])] })} /></label>
                <label>Timeout in seconds<input type="number" min={1} max={600} value={m.test_command.timeout_seconds} onChange={event => updateCommand({ timeout_seconds: Number(event.target.value) })} /></label>
                <p className="field-note">The model cannot add arguments. Repository-controlled scripts may still execute code from a writable mount.</p>
              </div>}
              {m.capabilities.includes('custom_commands') && <div className="subform">
                <div className="subform-heading"><Terminal size={16} /><h3>Custom fixed commands</h3></div>
                {m.commands.map((command, index) => <div className="skill-card" key={index}>
                  <div className="subform-heading"><Terminal size={14} /><h3>Command {String(index + 1).padStart(2, '0')}</h3><button className="icon-button" aria-label={`Remove command ${index + 1}`} onClick={() => update({ commands: m.commands.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} /></button></div>
                  <label>Command name<input className="mono" value={command.name} onChange={event => updateCommandItem(index, { name: event.target.value })} placeholder="letters, numbers, hyphens, underscores" /></label>
                  <label>Description<input value={command.description} onChange={event => updateCommandItem(index, { description: event.target.value })} /></label>
                  <label>Executable<input className="mono" value={command.argv[0] || ''} onChange={event => updateCommandItem(index, { argv: [event.target.value, ...command.argv.slice(1)] })} /></label>
                  <label>Arguments <span className="label-note">one per line; passed literally</span><textarea className="mono short-textarea" value={command.argv.slice(1).join('\n')} onChange={event => updateCommandItem(index, { argv: [command.argv[0], ...(event.target.value ? event.target.value.split('\n') : [])] })} /></label>
                  <label>Working directory<select value={command.working_directory} onChange={event => updateCommandItem(index, { working_directory: event.target.value })}>{m.mounts.map(mount => <option key={mount.target}>{mount.target}</option>)}</select></label>
                  <label>Timeout in seconds<input type="number" min={1} max={600} value={command.timeout_seconds} onChange={event => updateCommandItem(index, { timeout_seconds: Number(event.target.value) })} /></label>
                </div>)}
                <button className="add-button" onClick={() => update({ commands: [...m.commands, { name: `command-${m.commands.length + 1}`, description: 'Describe what this command does.', argv: ['echo', 'hello'], working_directory: m.mounts[0]?.target || 'repo', timeout_seconds: 120 }] })}><Plus size={15} /> Add a command</button>
              </div>}
              {m.capabilities.includes('build_test') && m.build_test_steps && <div className="subform">
                <div className="subform-heading"><Terminal size={16} /><h3>Build &amp; test steps</h3></div>
                {(['format', 'build', 'test'] as const).map(stepKey => {
                  const step = m.build_test_steps![stepKey];
                  const stepLabel = stepKey.charAt(0).toUpperCase() + stepKey.slice(1);
                  return <div className="skill-card" key={stepKey}>
                    <div className="subform-heading"><Terminal size={14} /><h3>{stepLabel}</h3>{step && <button className="icon-button" aria-label={`Remove ${stepKey} step`} onClick={() => updateBuildTestStep(stepKey, null)}><Trash2 size={15} /></button>}</div>
                    {step ? <>
                      <div className="field-grid">
                        <label>Executable<input className="mono" value={step.argv[0] || ''} onChange={event => updateBuildTestStep(stepKey, { argv: [event.target.value, ...step.argv.slice(1)] })} /></label>
                        <label>Working directory<select value={step.working_directory} onChange={event => updateBuildTestStep(stepKey, { working_directory: event.target.value })}>{m.mounts.map(mount => <option key={mount.target}>{mount.target}</option>)}</select></label>
                      </div>
                      <label>Arguments <span className="label-note">one per line; passed literally</span><textarea className="mono short-textarea" value={step.argv.slice(1).join('\n')} onChange={event => updateBuildTestStep(stepKey, { argv: [step.argv[0], ...(event.target.value ? event.target.value.split('\n') : [])] })} /></label>
                      <label>Timeout in seconds<input type="number" min={1} max={600} value={step.timeout_seconds} onChange={event => updateBuildTestStep(stepKey, { timeout_seconds: Number(event.target.value) })} /></label>
                    </> : <button className="add-button" onClick={() => updateBuildTestStep(stepKey, {})}><Plus size={15} /> Add {stepKey} step</button>}
                  </div>;
                })}
              </div>}
              {m.capabilities.includes('bash_commands') && <div className="subform">
                <div className="subform-heading"><Terminal size={16} /><h3>Allowed Bash programs</h3></div>
                <div className="bash-program-grid">
                  {catalog.bash_programs.map(program => {
                    const selected = m.allowed_bash_programs.includes(program);
                    return <label className="bash-program-option" key={program}>
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => update({
                          allowed_bash_programs: selected
                            ? m.allowed_bash_programs.filter(item => item !== program)
                            : [...m.allowed_bash_programs, program],
                        })}
                      />
                      <span>{program}</span>
                    </label>;
                  })}
                </div>
                <p className="field-note">The agent can run any command whose leading program is checked here, including compound or piped commands. This is a capability grant, not a suggestion.</p>
              </div>}
              {(m.capabilities.includes('git_read') || m.capabilities.includes('git_write') || m.capabilities.includes('gitlab_mr')) && <div className="subform">
                <label>Git working directory<select value={m.git_directory || ''} onChange={event => update({ git_directory: event.target.value })}>{m.mounts.map(mount => <option key={mount.target}>{mount.target}</option>)}</select></label>
                <p className="field-note">Read operations are fixed. Commit changes adds one bounded message; hooks and signing are disabled and tracked changes are committed with <code>git commit -a</code>.</p>
              </div>}
              </>}
            </section>

            <section className="form-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('workspace')} onClick={() => toggleSection('workspace')}><div><span className="section-number">05</span><h2>Define its workspace</h2></div><span className="section-title-trailing"><FolderOpen size={19} /><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('workspace') && <>
              <p className="section-description">The runtime exposes only these host paths through mount aliases.</p>
              {m.mounts.map((mount, index) => <div className="mount-card" key={index}>
                <div className="mount-heading"><span><FolderOpen size={14} /> MOUNT {String(index + 1).padStart(2, '0')}</span><button className="icon-button" aria-label={`Remove mount ${index + 1}`} onClick={() => update({ mounts: m.mounts.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} /></button></div>
                <label>Host path<input className="mono" value={mount.host_path} onChange={event => updateMount(index, { host_path: event.target.value })} placeholder="/Users/you/project/src" /></label>
                <div className="field-grid"><label>Tool path alias<input className="mono" value={mount.target} onChange={event => updateMount(index, { target: event.target.value })} /></label>
                  <label>Access<select aria-label={`Mount ${index + 1} access`} value={mount.read_only ? 'ro' : 'rw'} onChange={event => updateMount(index, { read_only: event.target.value === 'ro' })}><option value="ro">Read only</option><option value="rw">Read & write</option></select></label></div>
                <div className="mount-destination"><ChevronRight size={12} /><code>{mount.target}/path/to/file</code><LockKeyhole size={12} /></div>
              </div>)}
              <button className="add-button" onClick={() => update({ mounts: [...m.mounts, { host_path: '/absolute/path/to/source', target: `source-${m.mounts.length + 1}`, read_only: true }] })}><Plus size={15} /> Add a mount</button>
              <p className="scope-note"><ShieldCheck size={15} /><span>Mounts are a software boundary, not an OS sandbox. Keep paths narrow; even read-only mounts expose every file under the selected directory.</span></p>
              </>}
            </section>

            <section className="form-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('behavior')} onClick={() => toggleSection('behavior')}><div><span className="section-number">06</span><h2>Shape its behavior</h2></div><span className="section-title-trailing"><Cpu size={19} /><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('behavior') && <>
              <div className="label-row"><label htmlFor="prompt-override">Full system prompt</label><button className="text-button" onClick={() => void resetPromptToGenerated()}><RotateCcw size={12} /> Reset to generated</button></div>
              <textarea id="prompt-override" className="custom-prompt-editor" maxLength={20000} value={m.prompt.override ?? ''} onChange={event => update({ prompt: { ...m.prompt, override: event.target.value } })} />
              <p className="field-note">This text replaces the generated title, capabilities, workspace, behavior, and skill sections verbatim.</p>
              </>}
            </section>

            <section className="form-section">
              <button type="button" className="section-title section-toggle" aria-expanded={!collapsedSections.has('mcp')} onClick={() => toggleSection('mcp')}><div><span className="section-number">07</span><h2>MCP tools & skills</h2></div><span className="section-title-trailing"><Puzzle size={19} /><ChevronRight className="section-caret" size={16} /></span></button>
              {!collapsedSections.has('mcp') && <>
              <p className="section-description">Import captured MCP schemas and expose individual tools. Launch settings retain environment-variable references only.</p>
              <input ref={snapshotInput} type="file" accept=".json,application/json" hidden onChange={event => { void importSnapshot(event.target.files?.[0]); event.target.value = ''; }} />
              {m.mcp_servers.map((server, index) => <div className="mcp-card" key={server.name}>
                <div className="subform-heading"><Puzzle size={16} /><h3>{server.name}</h3><span className="count-badge">{server.selected_tools.length} / {server.snapshot.length}</span><button className="icon-button" aria-label={`Remove MCP server ${server.name}`} onClick={() => update({ mcp_servers: m.mcp_servers.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={14} /></button></div>
                <code className="server-address">{server.type === 'stdio' ? [server.command, ...server.args].join(' ') : server.url}</code>
                {server.snapshot.map(tool => <label className="tool-checkbox" key={tool.name}><input type="checkbox" checked={server.selected_tools.includes(tool.name)} onChange={() => update({ mcp_servers: m.mcp_servers.map((item, itemIndex) => itemIndex === index ? { ...item, selected_tools: item.selected_tools.includes(tool.name) ? item.selected_tools.filter(name => name !== tool.name) : [...item.selected_tools, tool.name] } : item) })} /><span><strong>{tool.name}</strong><small>{tool.description || 'No description captured.'}</small></span></label>)}
              </div>)}
              <button className="add-button" onClick={() => snapshotInput.current?.click()}><Upload size={15} /> Import MCP snapshot</button>
              <details className="snapshot-help"><summary><ChevronRight size={14} /> Capture a server snapshot</summary><p>Run the snapshot command locally, inspect its JSON, then import it here.</p><code>python -m builder.snapshot --name my-server --stdio npx -- -y @example/server &gt; my-server.json</code></details>
              <div className="skills-heading"><h3>Inline skills</h3><span>Prompt guidance, not executable code</span></div>
              {m.skills.map((skill, index) => <div className="skill-card" key={index}>
                <div className="field-grid"><label>Skill name<input className="mono" value={skill.name} onChange={event => updateSkill(index, { name: event.target.value })} /></label><button className="icon-button" aria-label={`Remove skill ${index + 1}`} onClick={() => update({ skills: m.skills.filter((_, itemIndex) => itemIndex !== index) })}><Trash2 size={15} /></button></div>
                <label>Description<input value={skill.description} onChange={event => updateSkill(index, { description: event.target.value })} /></label>
                <label>Instructions<textarea value={skill.content} onChange={event => updateSkill(index, { content: event.target.value })} /></label>
              </div>)}
              <button className="add-button" onClick={() => update({ skills: [...m.skills, { name: `skill-${m.skills.length + 1}`, description: 'Focused workflow guidance.', content: 'Follow these steps carefully.' }] })}><Plus size={15} /> Add a skill</button>
              </>}
            </section>
          </>}
        </div>

        <aside className="preview-column">
          <div className="preview-panel">
            <div className="preview-heading"><div><span className="preview-dot" /><h2>Compiled runtime</h2></div><span>{current ? 'VALIDATED' : 'COMPILING'}</span></div>
            <div className="preview-tabs" role="tablist" aria-label="Compiled output">
              <button role="tab" aria-selected={previewTab === 'prompt'} className={previewTab === 'prompt' ? 'active' : ''} onClick={() => setPreviewTab('prompt')}><FileCode2 size={13} /> Prompt</button>
              <button role="tab" aria-selected={previewTab === 'tools'} className={previewTab === 'tools' ? 'active' : ''} onClick={() => setPreviewTab('tools')}><Terminal size={13} /> Tools</button>
              <button role="tab" aria-selected={previewTab === 'config'} className={previewTab === 'config' ? 'active' : ''} onClick={() => setPreviewTab('config')}><Braces size={13} /> Config</button>
              <button className="copy-preview" aria-label="Copy preview" disabled={!preview} onClick={() => void copy(previewText)}><Copy size={13} /></button>
            </div>
            {error ? <div className="preview-error" role="alert"><ShieldCheck size={24} /><h3>Manifest needs attention</h3><p>{error}</p><span>The last valid compiled output is withheld until this manifest passes validation.</span></div>
              : previewTab === 'tools' && preview ? <div className={`preview-content compiled-tools ${current ? '' : 'is-stale'}`}>{preview.tools.length ? preview.tools.map(tool => <details className="compiled-tool" key={tool.name}><summary><span><code>{tool.name}</code><small>{tool.kind}</small></span><ChevronRight size={14} /></summary><p>{tool.description}</p><pre>{JSON.stringify(tool, null, 2)}</pre></details>) : <p className="empty-preview">No tools selected.</p>}</div>
                : <div className={`preview-content ${current ? '' : 'is-stale'}`}><CodePreview text={previewText} /></div>}
            {!error && preview && <div className="preview-footer"><span><CircleCheck size={13} /> Schema-valid output</span><span>{preview.provider.kind} · {preview.model}</span></div>}
            <div className="bundle-summary"><div><strong>{preview?.tools.length || 0}</strong><span>tools</span></div><div><strong>{preview?.mounts.length || 0}</strong><span>mounts</span></div><div><strong>{preview?.mcpServers.length || 0}</strong><span>MCP servers</span></div></div>
            <div className="export-area"><div className="export-actions"><button className="download-button" disabled={!current} onClick={downloadManifest}><ArrowDownToLine size={17} /> Download manifest <span>.json</span></button>
              {matchedBinary
                ? <a className="download-button" href={`/dl/cli/${matchedBinary.filename}`} download><ArrowDownToLine size={17} /> Download CLI <span>{matchedBinary.os}/{matchedBinary.arch}</span></a>
                : <button className="download-button" disabled><ArrowDownToLine size={17} /> No CLI build exists yet for <span>{detectedOS || 'this OS'}</span></button>}
            </div>
              <p><ShieldCheck size={12} /> Downloaded in the browser; no ZIP or generated installer.</p></div>
          </div>
        </aside>
      </div>

      <footer className="site-footer"><span><span className="tiny-mark">H</span> Harness Pro</span><span>Provider-agnostic · bounded tools · deterministic manifests</span></footer>
    </main>
    {showHandoff && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setShowHandoff(false); }}>
      <section className="handoff handoff-modal" role="dialog" aria-modal="true" aria-labelledby="handoff-title">
        <div className="handoff-heading"><Terminal size={17} /><h3 id="handoff-title">{downloaded ? 'Manifest ready to run' : 'Standalone CLI handoff'}</h3>
          <button ref={modalClose} className="modal-close" aria-label="Close how it works" onClick={() => setShowHandoff(false)}><X size={18} /></button></div>
        {matchedBinary ? <>
          <p>Download the standalone CLI for {matchedBinary.os}/{matchedBinary.arch}, then compile the same deterministic runtime config or start one headless run. Set <code>{m.api_key_env || 'no API key variable'}</code> in your local environment when required.</p>
          <a className="download-button" href={`/dl/cli/${matchedBinary.filename}`} download><ArrowDownToLine size={17} /> Download CLI <span>{matchedBinary.os}/{matchedBinary.arch}</span></a>
        </> : <p>From this repository, compile the same deterministic runtime config or start one headless run. Set <code>{m.api_key_env || 'no API key variable'}</code> in your local environment when required.</p>}
        {commands.map(command => <div className="command-line" key={command}><span>$</span><code>{command}</code><button aria-label="Copy command" onClick={() => void copy(command)}><Copy size={13} /></button></div>)}
      </section>
    </div>}
  </>;
}
