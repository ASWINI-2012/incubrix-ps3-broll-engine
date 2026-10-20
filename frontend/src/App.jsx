import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  AudioLines,
  BookOpen,
  Check,
  ChevronDown,
  CircleAlert,
  Clapperboard,
  Clock3,
  Download,
  ExternalLink,
  FileText,
  Film,
  FolderKanban,
  Gauge,
  Home,
  LoaderCircle,
  Menu,
  Mic2,
  MonitorPlay,
  Plus,
  Search,
  Settings,
  Sparkles,
  Upload,
  WandSparkles,
  X,
} from "lucide-react";
import { API_URL, checkReadiness, downloadMediaDataset, fileUrl, getErrorMessage, getJob, getMediaLibrary, getTimeline, getVoices, scanMediaLibrary, submitJob } from "./api.js";
import { getCurrentJobVideo, getCurrentJobVideos, outputRatio } from "./video.js";

const JOB_STORAGE_KEY = "incubrix.studio.jobs";
const RATIOS = [
  { value: "16:9", label: "Landscape", detail: "YouTube · widescreen" },
  { value: "9:16", label: "Portrait", detail: "Reels · vertical" },
  { value: "1:1", label: "Square", detail: "Social · square" },
];
const NAV_ITEMS = [
  { id: "dashboard", label: "Dashboard", icon: Home },
  { id: "studio", label: "Create Video", icon: Clapperboard },
  { id: "projects", label: "Projects", icon: FolderKanban },
  { id: "library", label: "Video Library", icon: MonitorPlay },
  { id: "media", label: "Media Library", icon: Film },
];
const PAGE_DETAILS = {
  dashboard: ["Dashboard", "Your creative workspace"],
  studio: ["Create Video", "Studio / New creation"],
  projects: ["Projects", "Workspace / Projects"],
  library: ["Video Library", "Workspace / Library"],
  media: ["Media Library", "Workspace / Media"],
  settings: ["Settings", "Workspace / Settings"],
};

function readStoredJobs() {
  try {
    const stored = JSON.parse(localStorage.getItem(JOB_STORAGE_KEY) || "[]");
    return Array.isArray(stored)
      ? stored.filter((item) => typeof item?.job_id === "string").slice(0, 30)
      : [];
  } catch {
    return [];
  }
}

function saveStoredJob(entry) {
  const previous = readStoredJobs().filter((item) => item.job_id !== entry.job_id);
  try {
    localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify([entry, ...previous].slice(0, 30)));
    return true;
  } catch {
    return false;
  }
}

function formatDate(value) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatDuration(value) {
  if (value === null || value === undefined || value === "") return null;
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue)) return null;
  const seconds = Math.floor(numericValue);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function getStatusLabel(status) {
  return ({ queued: "Queued", processing: "In progress", completed: "Ready", failed: "Failed" })[status] || status || "Unknown";
}

function StatusBadge({ status }) {
  return <span className={`status-badge status-${status || "unknown"}`}><i />{getStatusLabel(status)}</span>;
}

function EmptyState({ icon: Icon = Film, title, children, action }) {
  return (
    <div className="empty-state">
      <span className="empty-icon"><Icon size={21} /></span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}

function App() {
  const [page, setPage] = useState("dashboard");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [readiness, setReadiness] = useState(null);
  const [voices, setVoices] = useState([]);
  const [voiceError, setVoiceError] = useState("");
  const [jobs, setJobs] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const [activeJob, setActiveJob] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [timelineError, setTimelineError] = useState("");
  const [pollError, setPollError] = useState("");
  const [formError, setFormError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [videoError, setVideoError] = useState("");
  const [videoRetry, setVideoRetry] = useState(0);
  const [search, setSearch] = useState("");
  const [selectedRatio, setSelectedRatio] = useState("16:9");

  const [mode, setMode] = useState("script");
  const [script, setScript] = useState("");
  const [manualScenes, setManualScenes] = useState("");
  const [audioScenes, setAudioScenes] = useState("");
  const [audioFile, setAudioFile] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [title, setTitle] = useState("");
  const [visualStyle, setVisualStyle] = useState("Cinematic");
  const [visualSource, setVisualSource] = useState("footage");
  const [ratios, setRatios] = useState(["16:9"]);
  const [voice, setVoice] = useState("");
  const [captions, setCaptions] = useState(true);
  const [avoid, setAvoid] = useState("");

  const wordCount = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const sceneLines = useMemo(
    () => manualScenes.split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
    [manualScenes],
  );
  const currentPage = PAGE_DETAILS[page] || PAGE_DETAILS.dashboard;
  const outputVideos = getCurrentJobVideos(activeJob);
  const selectedVideo = getCurrentJobVideo(activeJob, selectedRatio);
  const filteredJobs = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return jobs;
    return jobs.filter((job) =>
      `${job.localTitle || ""} ${job.job_id} ${job.status}`.toLowerCase().includes(query),
    );
  }, [jobs, search]);

  const updateKnownJob = useCallback((jobData, localTitle) => {
    setJobs((existing) => {
      const old = existing.find((item) => item.job_id === jobData.job_id);
      const next = { ...old, ...jobData, localTitle: localTitle || old?.localTitle };
      return [next, ...existing.filter((item) => item.job_id !== jobData.job_id)];
    });
  }, []);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const status = await checkReadiness();
        if (alive) setReadiness(status);
      } catch {
        if (alive) setReadiness({ ready: false, status: "unavailable", checks: {} });
      }
    };
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    getVoices()
      .then((items) => { if (alive) setVoices(items); })
      .catch((error) => { if (alive) setVoiceError(getErrorMessage(error, "Local system voices are unavailable.")); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    const knownJobs = readStoredJobs();
    if (!knownJobs.length) {
      setHistoryLoading(false);
      return () => { alive = false; };
    }
    Promise.allSettled(knownJobs.map(async (entry) => ({
      ...await getJob(entry.job_id),
      localTitle: entry.title || "",
    })))
      .then((results) => {
        if (!alive) return;
        const loaded = results
          .filter((result) => result.status === "fulfilled")
          .map((result) => result.value);
        setJobs(loaded);
        if (loaded.length) setActiveJob(loaded[0]);
        const unavailable = results.length - loaded.length;
        if (unavailable) {
          setHistoryError(`${unavailable} saved job${unavailable === 1 ? "" : "s"} could not be loaded from the backend.`);
        }
        setHistoryLoading(false);
      });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!activeJob?.job_id || !["queued", "processing"].includes(activeJob.status)) return undefined;
    let alive = true;
    let timer;
    const poll = async () => {
      try {
        const data = await getJob(activeJob.job_id);
        if (!alive) return;
        setPollError("");
        setActiveJob((current) => current?.job_id === data.job_id ? { ...data, localTitle: current.localTitle } : current);
        updateKnownJob(data);
        if (data.status === "queued" || data.status === "processing") {
          timer = window.setTimeout(poll, 1800);
        }
      } catch (error) {
        if (alive) {
          setPollError(getErrorMessage(error, "The job status could not be refreshed."));
          timer = window.setTimeout(poll, 5000);
        }
      }
    };
    timer = window.setTimeout(poll, 1000);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [activeJob?.job_id, activeJob?.status, updateKnownJob]);

  useEffect(() => {
    if (activeJob?.status !== "completed" || !activeJob.job_id) {
      setTimeline(null);
      setTimelineError("");
      return undefined;
    }
    let alive = true;
    getTimeline(activeJob.job_id)
      .then((data) => { if (alive) setTimeline(data); })
      .catch((error) => {
        if (alive) setTimelineError(getErrorMessage(error, "Timeline details could not be loaded."));
      });
    return () => { alive = false; };
  }, [activeJob?.job_id, activeJob?.status]);

  function navigate(destination) {
    setPage(destination);
    setMobileNavOpen(false);
  }

  function chooseJob(job) {
    setActiveJob(job);
    setPollError("");
    setSelectedRatio(outputRatio(job.result?.videos?.[0]) || "16:9");
    setVideoError("");
    navigate("studio");
  }

  function toggleRatio(value) {
    setRatios([value]);
    setSelectedRatio(value);
    setFormError("");
  }

  function getInputSummary() {
    if (mode === "script") return `${wordCount} words`;
    if (mode === "scenes") return `${sceneLines.length} scene${sceneLines.length === 1 ? "" : "s"}`;
    return audioFile ? audioFile.name : audioUrl.trim() ? "Audio URL" : "No audio selected";
  }

  async function createVideo(event) {
    event.preventDefault();
    setFormError("");
    setVideoError("");
    if (!readiness?.ready) {
      setFormError("The rendering engine is not ready. Check the backend connection before starting a job.");
      return;
    }
    if (ratios.length !== 1) {
      setFormError("Choose exactly one output format for this video.");
      return;
    }
    if (mode === "script" && !script.trim()) {
      setFormError("Add a script before generating your video.");
      return;
    }
    if (mode === "script" && script.trim().length > 20000) {
      setFormError("Scripts must be 20,000 characters or fewer.");
      return;
    }
    if (mode === "scenes" && (!sceneLines.length || sceneLines.length > 100 || sceneLines.some((line) => line.length > 2000))) {
      setFormError("Add 1–100 scenes, with no scene longer than 2,000 characters.");
      return;
    }
    if (mode === "audio" && !audioFile && !audioUrl.trim()) {
      setFormError("Choose an MP3, WAV, or M4A file, or provide an HTTPS audio URL.");
      return;
    }
    if (mode === "audio" && audioFile && audioUrl.trim()) {
      setFormError("Choose an audio file or an audio URL, not both.");
      return;
    }
    if (mode === "audio" && audioFile && !/\.(mp3|wav|m4a)$/i.test(audioFile.name)) {
      setFormError("Audio uploads must be MP3, WAV, or M4A.");
      return;
    }
    if (mode === "audio" && audioFile?.size > 50 * 1024 * 1024) {
      setFormError("The default backend upload limit is 50 MB. Choose a smaller audio file.");
      return;
    }
    if (mode === "audio" && audioUrl.trim()) {
      try {
        if (new URL(audioUrl.trim()).protocol !== "https:") throw new Error();
      } catch {
        setFormError("Enter a valid HTTPS audio URL.");
        return;
      }
    }
    const avoidTerms = avoid.split(",").map((term) => term.trim()).filter(Boolean);
    if (avoidTerms.length > 30 || avoidTerms.some((term) => term.length > 100)) {
      setFormError("Add up to 30 avoid terms, each 100 characters or fewer.");
      return;
    }
    const common = {
      aspect_ratios: ratios,
      captions_enabled: captions,
      visual_style: visualStyle.toLowerCase(),
      visual_source: visualSource,
      avoid: avoidTerms,
      title: title.trim() || undefined,
      voice: voice || undefined,
    };
    let body;
    let multipart = false;
    if (mode === "audio") {
      multipart = true;
      body = new FormData();
      if (audioFile) body.append("audio", audioFile);
      if (audioUrl.trim()) body.append("audio_url", audioUrl.trim());
      Object.entries(common).filter(([, value]) => value !== undefined).forEach(([key, value]) => {
        body.append(key, typeof value === "string" ? value : JSON.stringify(value));
      });
      const scenes = audioScenes.split(/\r?\n/).map((text) => text.trim()).filter(Boolean).map((text) => ({ text }));
      if (scenes.length > 100 || scenes.some((scene) => scene.text.length > 2000)) {
        setFormError("Add up to 100 audio scene notes, each 2,000 characters or fewer.");
        return;
      }
      if (scenes.length) body.append("scenes", JSON.stringify(scenes));
    } else if (mode === "scenes") {
      body = { ...common, scenes: sceneLines.map((text) => ({ text })) };
    } else {
      body = { ...common, script: script.trim() };
    }

    setSubmitting(true);
    setActiveJob(null);
    setTimeline(null);
    setTimelineError("");
    setHistoryError("");
    setPollError("");
    try {
      const created = await submitJob(body, multipart);
      const stored = saveStoredJob({ job_id: created.job_id, title: title.trim() });
      if (!stored) setStorageNotice("This browser could not save the job to its local history. It remains available until you leave this page.");
      else setStorageNotice("");
      const nextJob = { ...created, localTitle: title.trim() };
      setActiveJob(nextJob);
      updateKnownJob(nextJob, title.trim());
      if (created.status === "failed") {
        setFormError(created.error?.message || "The backend could not start this job.");
      }
    } catch (error) {
      setFormError(getErrorMessage(error, "The video job could not be created."));
    } finally {
      setSubmitting(false);
    }
  }

  const startNewVideo = (nextMode = "script") => {
    setActiveJob(null);
    setMode(nextMode);
    setScript("");
    setManualScenes("");
    setAudioScenes("");
    setAudioFile(null);
    setAudioUrl("");
    setTitle("");
    setAvoid("");
    setRatios(["16:9"]);
    setSelectedRatio("16:9");
    setTimeline(null);
    setTimelineError("");
    setPollError("");
    setFormError("");
    setVideoError("");
    navigate("studio");
  };
  const startScriptVideo = () => {
    startNewVideo("script");
  };
  const startAudioVideo = () => {
    startNewVideo("audio");
  };
  const editAndRetry = () => {
    setActiveJob(null);
    setTimeline(null);
    setTimelineError("");
    setPollError("");
    setFormError("");
    setVideoError("");
  };

  const titleFor = (job) => job.localTitle || `Video · ${job.job_id.slice(0, 8)}`;

  return (
    <div className="app-shell">
      {mobileNavOpen && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMobileNavOpen(false)} />}
      <aside className={`sidebar ${mobileNavOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-brand">
          <span className="brand-mark"><Clapperboard size={19} strokeWidth={2.1} /></span>
          <span className="brand-name">IncuBrix <strong>Studio</strong></span>
          <button className="mobile-close icon-button" aria-label="Close menu" onClick={() => setMobileNavOpen(false)}><X size={18} /></button>
        </div>
        <div className="workspace-switcher"><span className="workspace-avatar">I</span><span><strong>Creative workspace</strong><small>Personal studio</small></span><ChevronDown size={14} /></div>
        <nav className="primary-nav" aria-label="Main navigation">
          <span className="nav-section-label">WORKSPACE</span>
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button key={id} className={`nav-link ${page === id ? "nav-link-active" : ""}`} onClick={() => navigate(id)} aria-current={page === id ? "page" : undefined}>
              <Icon size={17} /><span>{label}</span>
              {id === "projects" && jobs.length > 0 && <span className="nav-count">{jobs.length}</span>}
            </button>
          ))}
          <span className="nav-section-label nav-tools-label">PREFERENCES</span>
          <button className={`nav-link ${page === "settings" ? "nav-link-active" : ""}`} onClick={() => navigate("settings")} aria-current={page === "settings" ? "page" : undefined}><Settings size={17} /><span>Settings</span></button>
        </nav>
        <div className="sidebar-bottom">
          <div className={`sidebar-engine ${readiness?.ready ? "engine-ready" : readiness ? "engine-offline" : ""}`}>
            <span className="engine-icon"><Activity size={15} /></span>
            <span><strong>Render engine</strong><small>{readiness?.ready ? "Connected and ready" : readiness ? "Connection needs attention" : "Checking connection…"}</small></span>
            <i />
          </div>
          <div className="sidebar-user"><span className="user-avatar">IB</span><span><strong>IncuBrix Studio</strong><small>Local workspace</small></span><ChevronDown size={14} /></div>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="topbar-left">
            <button className="mobile-menu icon-button" aria-label="Open navigation" onClick={() => setMobileNavOpen(true)}><Menu size={19} /></button>
            <div className="breadcrumbs"><span>Studio</span><span className="breadcrumb-slash">/</span><strong>{currentPage[0]}</strong></div>
          </div>
          <div className="topbar-actions">
            <div className={`connection-pill ${readiness?.ready ? "connection-online" : readiness ? "connection-offline" : ""}`} role="status">
              <i />{readiness?.ready ? "Engine online" : readiness ? "Engine offline" : "Connecting"}
            </div>
            <a className="topbar-docs" href={`${API_URL}/docs`} target="_blank" rel="noreferrer" aria-label="Open API documentation"><BookOpen size={16} /><span>API docs</span><ExternalLink size={12} /></a>
          </div>
        </header>

        <main className="content-area">
          {page === "dashboard" && (
            <Dashboard
              jobs={jobs}
              loading={historyLoading}
              error={historyError}
              onCreate={startNewVideo}
              onCreateScript={startScriptVideo}
              onCreateAudio={startAudioVideo}
              onOpenJob={chooseJob}
              onGoProjects={() => navigate("projects")}
              titleFor={titleFor}
            />
          )}
          {page === "studio" && (
            <StudioPage
              mode={mode} setMode={(nextMode) => { setMode(nextMode); setFormError(""); }}
              script={script} setScript={setScript} wordCount={wordCount}
              manualScenes={manualScenes} setManualScenes={setManualScenes} sceneLines={sceneLines}
              audioScenes={audioScenes} setAudioScenes={setAudioScenes}
              audioFile={audioFile} setAudioFile={setAudioFile}
              audioUrl={audioUrl} setAudioUrl={setAudioUrl}
              title={title} setTitle={setTitle}
              visualStyle={visualStyle} setVisualStyle={setVisualStyle}
              visualSource={visualSource} setVisualSource={setVisualSource}
              ratios={ratios} toggleRatio={toggleRatio}
              voice={voice} setVoice={setVoice} voices={voices} voiceError={voiceError}
              captions={captions} setCaptions={setCaptions}
              avoid={avoid} setAvoid={setAvoid}
              error={formError} submitting={submitting} onSubmit={createVideo}
              inputSummary={getInputSummary()}
              readiness={readiness}
              activeJob={activeJob} timeline={timeline} timelineError={timelineError} connectionError={pollError}
              selectedRatio={selectedRatio} setSelectedRatio={setSelectedRatio}
              selectedVideo={selectedVideo} videoError={videoError}
              onVideoError={() => setVideoError("The generated video could not be loaded. The output may be unavailable; retry loading or download the file.")}
              onVideoRetry={() => { setVideoError(""); setVideoRetry((value) => value + 1); }}
              videoRetry={videoRetry}
              onCreateAnother={editAndRetry}
              storageNotice={storageNotice}
            />
          )}
          {page === "projects" && (
            <CollectionPage
              kind="projects" jobs={filteredJobs} loading={historyLoading} error={historyError}
              search={search} setSearch={setSearch} titleFor={titleFor} onCreate={startNewVideo} onOpenJob={chooseJob}
            />
          )}
          {page === "library" && (
            <CollectionPage
              kind="library" jobs={filteredJobs} loading={historyLoading} error={historyError}
              search={search} setSearch={setSearch} titleFor={titleFor} onCreate={startNewVideo} onOpenJob={chooseJob}
            />
          )}
          {page === "media" && <MediaLibraryPage />}
          {page === "settings" && <SettingsPage readiness={readiness} voices={voices} voiceError={voiceError} />}
          <footer className="page-footer"><span>IncuBrix Studio <span className="footer-divider">·</span> Local workspace</span><a href={`${API_URL}/docs`} target="_blank" rel="noreferrer">Backend API documentation <ExternalLink size={12} /></a></footer>
        </main>
      </div>
    </div>
  );
}

function Dashboard({ jobs, loading, error, onCreate, onCreateScript, onCreateAudio, onOpenJob, onGoProjects, titleFor }) {
  const completed = jobs.reduce((total, job) => total + (job.result?.videos?.length || 0), 0);
  const running = jobs.filter((job) => ["queued", "processing"].includes(job.status)).length;
  return (
    <div className="page-view dashboard-page">
      <section className="welcome-row">
        <div><div className="eyebrow"><Sparkles size={14} /> YOUR CREATIVE WORKSPACE</div><h1>Make something <span>remarkable.</span></h1><p>Turn a script, voiceover, or scene idea into a polished video.</p></div>
        <button className="primary-button" onClick={onCreate}><Plus size={17} />Create new video<ArrowRight size={15} /></button>
      </section>
      <section className="hero-panel">
        <div className="hero-copy">
          <span className="hero-kicker"><WandSparkles size={14} /> THE AI VIDEO STUDIO</span>
          <h2>Your next story,<br />beautifully in motion.</h2>
          <p>Start with your words. IncuBrix shapes scenes, sources visuals, and renders a video with narration and captions.</p>
          <button className="hero-action" onClick={onCreate}>Start creating <ArrowRight size={16} /></button>
          <div className="hero-detail"><span><Check size={13} /> Script, voiceover, or scenes</span><span><Check size={13} /> Export up to three formats</span></div>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="hero-frame frame-back"><span className="frame-sun" /><span className="frame-hill hill-back" /><span className="frame-hill hill-front" /></div>
          <div className="hero-frame frame-front"><div className="frame-topline"><i /><i /><i /><span>STORYBOARD / 01</span></div><div className="frame-scene"><span /><span /><i /></div><div className="frame-caption"><i /><i /></div></div>
          <div className="hero-glow" />
        </div>
        <div className="hero-index"><span>01</span><i /> CREATE · REFINE · SHARE</div>
      </section>
      <section className="stat-grid" aria-label="Workspace overview">
        <StatCard icon={FolderKanban} label="Known projects" value={loading ? "—" : jobs.length} detail="Saved in this browser" />
        <StatCard icon={MonitorPlay} label="Videos ready" value={loading ? "—" : completed} detail="Rendered MP4 outputs" />
        <StatCard icon={Gauge} label="In progress" value={loading ? "—" : running} detail="Queued or rendering now" />
      </section>
      <section className="dashboard-lower">
        <div className="panel recent-panel">
          <div className="panel-heading"><div><span className="panel-eyebrow">YOUR WORKSPACE</span><h2>Recent projects</h2></div><button className="text-button" onClick={onGoProjects}>All projects <ArrowRight size={14} /></button></div>
          {error && <p className="inline-notice notice-warning"><CircleAlert size={15} />{error}</p>}
          {loading ? <div className="loading-row"><LoaderCircle className="spin" size={17} />Checking locally saved jobs…</div>
            : jobs.length ? <div className="recent-list">{jobs.slice(0, 4).map((job) => <ProjectRow key={job.job_id} job={job} title={titleFor(job)} onClick={() => onOpenJob(job)} />)}</div>
              : <EmptyState icon={FolderKanban} title="No projects here yet">Your jobs will appear here after you create a video. The backend does not expose a global project history.</EmptyState>}
        </div>
        <aside className="panel next-panel">
          <span className="panel-eyebrow">START WITH WHAT YOU HAVE</span><h2>Every story has a starting point.</h2><p>Pick the input that fits your workflow. You can shape the look and output formats in the studio.</p>
          <button className="start-option" onClick={onCreateScript}><span className="start-option-icon"><FileText size={17} /></span><span><strong>Write a script</strong><small>From an idea to a narrated video</small></span><ArrowRight size={15} /></button>
          <button className="start-option" onClick={onCreateAudio}><span className="start-option-icon"><AudioLines size={17} /></span><span><strong>Bring a voiceover</strong><small>Upload audio or link a recording</small></span><ArrowRight size={15} /></button>
        </aside>
      </section>
    </div>
  );
}

function StatCard({ icon: Icon, label, value, detail }) {
  return <article className="stat-card"><span className="stat-icon"><Icon size={17} /></span><div><span className="stat-label">{label}</span><strong>{value}</strong><small>{detail}</small></div></article>;
}

function ProjectRow({ job, title, onClick }) {
  const video = job.result?.videos?.[0];
  return (
    <button className="project-row" onClick={onClick}>
      <span className="project-thumb"><Film size={19} /><span>{video ? outputRatio(video) : "STUDIO"}</span></span>
      <span className="project-info"><strong>{title}</strong><small>{formatDate(job.created_at)} <i /> {job.job_id.slice(0, 8)}</small></span>
      <StatusBadge status={job.status} /><ArrowRight className="project-arrow" size={15} />
    </button>
  );
}

function StudioPage(props) {
  const {
    mode, setMode, script, setScript, wordCount, manualScenes, setManualScenes, sceneLines,
    audioScenes, setAudioScenes, audioFile, setAudioFile, audioUrl, setAudioUrl, title, setTitle,
    visualStyle, setVisualStyle, visualSource, setVisualSource, ratios, toggleRatio, voice, setVoice, voices, voiceError,
    captions, setCaptions, avoid, setAvoid, error, submitting, onSubmit, inputSummary, readiness,
    activeJob, timeline, timelineError, connectionError, selectedRatio, setSelectedRatio, selectedVideo, videoError,
    onVideoError, onVideoRetry, videoRetry, onCreateAnother, storageNotice,
  } = props;
  const jobBusy = ["queued", "processing"].includes(activeJob?.status);
  const jobFailed = activeJob?.status === "failed";
  const styleOptions = ["Cinematic", "Documentary", "Educational", "Business"];
  return (
    <div className="page-view studio-page">
      <div className="page-title-row"><div><div className="eyebrow"><Clapperboard size={14} /> VIDEO CREATION</div><h1>Build your next video.</h1><p>Configure your story, then let the engine take it from here.</p></div><span className="workspace-chip"><span />STUDIO WORKSPACE</span></div>
      <div className="studio-grid">
        <section className="panel composer-panel">
          <div className="panel-heading composer-heading"><div className="section-icon"><FileText size={17} /></div><div><h2>Project setup</h2><p>Start with the material you already have.</p></div><span className="step-number">01 <i /> 03</span></div>
          <div className="mode-tabs" role="tablist" aria-label="Choose source material">
            <button type="button" role="tab" aria-selected={mode === "script"} className={mode === "script" ? "active" : ""} onClick={() => setMode("script")}><FileText size={15} />Script</button>
            <button type="button" role="tab" aria-selected={mode === "audio"} className={mode === "audio" ? "active" : ""} onClick={() => setMode("audio")}><AudioLines size={15} />Voiceover</button>
            <button type="button" role="tab" aria-selected={mode === "scenes"} className={mode === "scenes" ? "active" : ""} onClick={() => setMode("scenes")}><Film size={15} />Scenes</button>
          </div>
          <form onSubmit={onSubmit} noValidate>
            {mode === "script" && <div className="field-block">
              <div className="field-label-row"><label htmlFor="script">YOUR SCRIPT</label><span>Max 20,000 characters</span></div>
              <textarea id="script" rows="7" maxLength={20000} value={script} onChange={(event) => setScript(event.target.value)} placeholder={"A powerful story starts with a single thought…\n\nWrite or paste your script. The engine will shape it into scenes, find visual matches, and create a voiceover."} />
              <div className="input-meta"><span>{wordCount} words <i /> {script.length.toLocaleString()} / 20,000 characters</span><span>Scene breaks follow sentence endings</span></div>
            </div>}
            {mode === "audio" && <div className="field-block">
              <div className="field-label-row"><label htmlFor="audio-file">YOUR VOICEOVER</label><span>MP3 · WAV · M4A</span></div>
              <label className={`upload-zone ${audioFile ? "has-file" : ""}`} htmlFor="audio-file">
                <input id="audio-file" type="file" accept=".mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/mp4" onChange={(event) => setAudioFile(event.target.files?.[0] || null)} />
                <span className="upload-icon">{audioFile ? <Check size={19} /> : <Upload size={19} />}</span>
                  <strong>{audioFile ? audioFile.name : "Choose an audio file"}</strong>
                <small>{audioFile ? `${(audioFile.size / 1024 / 1024).toFixed(1)} MB · ready to upload` : "Supported: MP3, WAV, M4A"}</small>
              </label>
              <div className="audio-url-separator"><span /> OR USE A LINK <span /></div>
              <div className="setting-field"><label htmlFor="audio-url">HTTPS AUDIO URL</label><input id="audio-url" type="url" value={audioUrl} onChange={(event) => setAudioUrl(event.target.value)} placeholder="https://example.com/voiceover.mp3" /></div>
              <div className="field-block compact-field"><div className="field-label-row"><label htmlFor="audio-scenes">OPTIONAL TRANSCRIPT / SCENE NOTES</label><span>One scene per line</span></div><textarea id="audio-scenes" rows="3" value={audioScenes} onChange={(event) => setAudioScenes(event.target.value)} placeholder="Add scene text to guide the visual search. Without notes, audio-only transcription requires the optional faster-whisper backend dependency." /></div>
            </div>}
            {mode === "scenes" && <div className="field-block">
              <div className="field-label-row"><label htmlFor="manual-scenes">SCENE DESCRIPTIONS</label><span>One scene per line · up to 100</span></div>
              <textarea id="manual-scenes" rows="7" value={manualScenes} onChange={(event) => setManualScenes(event.target.value)} placeholder={"Sunlight moves across the mountain valley.\nHands sketch a new idea in a notebook.\nA city skyline glows at the end of the day."} />
              <div className="input-meta"><span>{sceneLines.length} scenes <i /> max 2,000 characters each</span><span>Timing is allocated from narration length</span></div>
            </div>}

            <div className="form-section">
              <div className="section-label-row"><div><span className="section-step">02</span><span className="field-section-title">OUTPUT FORMAT</span></div><span>One MP4 per generation</span></div>
              <div className="ratio-grid">
                {RATIOS.map((ratio) => <button key={ratio.value} type="button" className={`ratio-option ${ratios.includes(ratio.value) ? "selected" : ""}`} onClick={() => toggleRatio(ratio.value)} aria-pressed={ratios.includes(ratio.value)}>
                  <span className={`ratio-icon ratio-${ratio.value.replace(":", "-")}`} /><span className="ratio-copy"><strong>{ratio.label}</strong><small>{ratio.detail}</small></span><span className="check-box">{ratios.includes(ratio.value) && <Check size={12} />}</span>
                </button>)}
              </div>
            </div>
            <div className="form-section settings-section">
              <div className="section-label-row"><div><span className="section-step">03</span><span className="field-section-title">FINISHING DETAILS</span></div><span>Fine-tune your output</span></div>
              <div className="settings-grid">
                <div className="setting-field"><label htmlFor="project-title">PROJECT TITLE <span>OPTIONAL</span></label><input id="project-title" maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Give your project a name" /></div>
                <div className="setting-field"><label htmlFor="visual-style">VISUAL STYLE</label><div className="select-wrap"><select id="visual-style" value={visualStyle} onChange={(event) => setVisualStyle(event.target.value)}>{styleOptions.map((style) => <option key={style}>{style}</option>)}</select><ChevronDown size={15} /></div></div>
                <div className="setting-field"><label htmlFor="visual-source">VISUAL SOURCE</label><div className="select-wrap"><select id="visual-source" value={visualSource} onChange={(event) => setVisualSource(event.target.value)}><option value="footage">Matched footage</option><option value="generated_images">AI-generated images</option></select><ChevronDown size={15} /></div>{visualSource === "generated_images" && <small className="voice-error">Requires a configured local image-generation model.</small>}</div>
                {mode !== "audio" && <div className="setting-field"><label htmlFor="narration-voice">NARRATION VOICE</label><div className="select-wrap"><select id="narration-voice" value={voice} onChange={(event) => setVoice(event.target.value)}><option value="">System default</option>{voices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><ChevronDown size={15} /></div>{voiceError && <small className="voice-error">{voiceError}</small>}</div>}
                <div className="setting-field"><label htmlFor="avoid-terms">WORDS / VISUALS TO AVOID <span>OPTIONAL</span></label><input id="avoid-terms" maxLength={500} value={avoid} onChange={(event) => setAvoid(event.target.value)} placeholder="Separate terms with commas" /></div>
              </div>
              <label className="caption-toggle"><span className="toggle-copy"><span className="toggle-icon"><FileText size={15} /></span><span><strong>Burn in captions</strong><small>Keep your video accessible and sound-off friendly.</small></span></span><input type="checkbox" checked={captions} onChange={(event) => setCaptions(event.target.checked)} /><span className="toggle-track" aria-hidden="true"><i /></span></label>
            </div>
            <div className="generation-summary"><span className="summary-icon"><Check size={15} /></span><div><strong>Generation summary</strong><small>{inputSummary} <i /> {ratios.length ? ratios.join(" · ") : "No format selected"} <i /> {visualStyle} <i /> captions {captions ? "on" : "off"}</small></div></div>
            {error && <div className="error-banner" role="alert"><CircleAlert size={16} /><span>{error}</span></div>}
            <button className="create-button" type="submit" disabled={submitting || jobBusy || !readiness?.ready}>
              {submitting || jobBusy ? <LoaderCircle className="spin" size={17} /> : <WandSparkles size={17} />}
              {submitting ? "Submitting job…" : jobBusy ? "Video is rendering…" : "Generate video"}<ArrowRight size={16} />
            </button>
            {!readiness?.ready && <p className="backend-hint"><CircleAlert size={13} />The backend render engine must be ready before generating.</p>}
            {storageNotice && <p className="inline-notice notice-warning"><CircleAlert size={14} />{storageNotice}</p>}
            <p className="privacy-note">Your source material stays on your configured backend. Footage and licence details are included when available.</p>
          </form>
        </section>
        <aside className="studio-side">
          <PreviewPanel
            activeJob={activeJob} selectedRatio={selectedRatio} setSelectedRatio={setSelectedRatio}
            selectedVideo={selectedVideo} videoError={videoError} onVideoError={onVideoError}
            onVideoRetry={onVideoRetry} videoRetry={videoRetry} onCreateAnother={onCreateAnother}
          />
          {activeJob && <JobStatusCard job={activeJob} />}
          {activeJob && connectionError && <p className="inline-notice notice-warning"><CircleAlert size={14} />{connectionError} The studio will retry polling this job.</p>}
          {(timeline || timelineError) && <TimelineCard timeline={timeline || { scenes: [] }} job={activeJob} error={timelineError} />}
          {!activeJob && <ProcessCard />}
        </aside>
      </div>
    </div>
  );
}

function PreviewPanel({ activeJob, selectedRatio, setSelectedRatio, selectedVideo, videoError, onVideoError, onVideoRetry, videoRetry, onCreateAnother }) {
  const activeRatio = outputRatio(selectedVideo) || selectedRatio;
  const busy = ["queued", "processing"].includes(activeJob?.status);
  return (
    <section className="panel preview-panel">
      <div className="panel-heading preview-heading"><div><span className="panel-eyebrow">CANVAS PREVIEW</span><h2>{activeJob?.status === "completed" ? "Your finished video" : busy ? "Rendering your video" : "Your video preview"}</h2></div>{activeJob && <StatusBadge status={activeJob.status} />}</div>
      <div className={`preview-canvas ratio-canvas-${activeRatio.replace(":", "-")}`}>
        {activeJob?.status === "completed" && selectedVideo ? (
          !selectedVideo.url ? <div className="preview-message"><span className="preview-message-icon"><CircleAlert size={22} /></span><strong>No playable MP4 link</strong><p>The backend completed the job but did not return a video URL.</p></div>
            : videoError ? <div className="preview-message"><span className="preview-message-icon"><CircleAlert size={22} /></span><strong>Video unavailable</strong><p>{videoError}</p><button className="secondary-button" onClick={onVideoRetry}>Retry loading</button></div>
              : <video key={`${selectedVideo.file}-${videoRetry}`} controls playsInline preload="metadata" src={fileUrl(selectedVideo.url)} onError={onVideoError} aria-label="Generated video preview">Your browser does not support MP4 playback.</video>
        ) : busy ? <div className="preview-message"><span className="render-orbit"><LoaderCircle className="spin" size={23} /></span><strong>Creating your cut</strong><p>The engine is processing your request. The job status is refreshed directly from the backend.</p></div>
          : activeJob?.status === "failed" ? <div className="preview-message"><span className="preview-message-icon error-tone"><CircleAlert size={22} /></span><strong>This generation did not finish</strong><p>{activeJob.error?.message || "The backend reported that the job failed."}</p><button className="secondary-button" onClick={onCreateAnother}>Edit and try again</button></div>
            : activeJob?.status === "completed" ? <div className="preview-message"><span className="preview-message-icon"><CircleAlert size={22} /></span><strong>No video output returned</strong><p>The backend marked this job complete but did not provide an MP4 result.</p></div>
            : <div className="preview-message"><div className="placeholder-art"><span className="art-orbit orbit-a" /><span className="art-orbit orbit-b" /><span className="art-studio-icon"><Clapperboard size={16} /></span><span className="art-frame"><i /><i /><i /></span></div><strong>Your canvas is ready</strong><p>Choose a source on the left, set your format, and generate a video to preview it here.</p></div>}
      </div>
      <div className="preview-toolbar">
        <div className="preview-format-tabs" aria-label="Preview aspect ratio">{RATIOS.map((ratio) => <button key={ratio.value} className={activeRatio === ratio.value ? "active" : ""} onClick={() => setSelectedRatio(ratio.value)} disabled={!activeJob?.result?.videos?.some((video) => outputRatio(video) === ratio.value)}>{ratio.value}</button>)}</div>
        {selectedVideo?.url && <a className="download-button" href={fileUrl(selectedVideo.url)} download={selectedVideo.file}><ArrowDownToLine size={15} /><span>Download</span></a>}
      </div>
      {selectedVideo && <div className="video-metadata"><span>{selectedVideo.width && selectedVideo.height ? `${selectedVideo.width} × ${selectedVideo.height}` : "Resolution unavailable"}</span><i />{formatDuration(selectedVideo.duration_seconds) && <><span>{formatDuration(selectedVideo.duration_seconds)}</span><i /></>}<span>{activeRatio}</span></div>}
    </section>
  );
}

function JobStatusCard({ job }) {
  const active = ["queued", "processing"].includes(job.status);
  return (
    <section className="panel job-status-panel">
      <div className="panel-heading compact-heading"><div><span className="panel-eyebrow">GENERATION JOB</span><h2>{job.localTitle || `Video · ${job.job_id.slice(0, 8)}`}</h2></div><StatusBadge status={job.status} /></div>
      <div className="job-stage-row"><span className={`stage-icon ${active ? "stage-active" : ""}`}>{active ? <LoaderCircle size={16} className="spin" /> : job.status === "completed" ? <Check size={16} /> : <CircleAlert size={16} />}</span><span><strong>{active ? (job.current_stage || "queued").replaceAll("_", " ") : getStatusLabel(job.status)}</strong><small>{active ? "Live status from the render engine" : `Job ${job.job_id.slice(0, 8)} · ${formatDate(job.created_at)}`}</small></span></div>
      {Number.isFinite(job.progress) && <div className="job-progress"><div className="progress-label"><span>BACKEND REPORTED PROGRESS</span><strong>{job.progress}%</strong></div><div className="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.max(0, Math.min(100, job.progress))}><i style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }} /></div></div>}
      {job.status === "completed" && job.result?.provider_mode === "local" && <div className="demo-notice"><strong>LOCAL CATALOGUE FOOTAGE</strong><p>Validated local clips were matched to the scenes. Source and licence details appear in the timeline.</p></div>}
      {job.error?.message && <div className="job-error"><CircleAlert size={14} />{job.error.message}</div>}
    </section>
  );
}

function TimelineCard({ timeline, job, error }) {
  const scenes = timeline.scenes || [];
  return (
    <section className="panel timeline-panel">
      <div className="panel-heading compact-heading"><div><span className="panel-eyebrow">EXPLAINABILITY</span><h2>Scene timeline <span className="heading-count">{scenes.length}</span></h2></div><a className="icon-link" href={fileUrl(job.result?.timeline_url || `/api/v1/jobs/${job.job_id}/timeline`)} target="_blank" rel="noreferrer" aria-label="Open timeline JSON"><ExternalLink size={15} /></a></div>
      {error && <p className="inline-notice notice-warning"><CircleAlert size={14} />{error}</p>}
      <div className="timeline-list">
        {scenes.map((scene, index) => <TimelineScene key={scene.scene_id || index} scene={scene} index={index} totalDuration={timeline.narration_duration_seconds} />)}
      </div>
      <div className="timeline-footer"><Clock3 size={13} /><span>Timing: {timeline.timing_method || "as reported by the backend"}</span></div>
      <div className="asset-links">
        {job.result?.captions_url && <a href={fileUrl(job.result.captions_url)} download><FileText size={14} />captions.srt</a>}
        {job.result?.manifest_url && <a href={fileUrl(job.result.manifest_url)} download><Download size={14} />manifest.json</a>}
        {job.result?.image_generation_report_url && <a href={fileUrl(job.result.image_generation_report_url)} download><Download size={14} />generated images report</a>}
      </div>
    </section>
  );
}

function TimelineScene({ scene, index, totalDuration }) {
  const clip = scene.clip || {};
  const score = clip.relevance_score !== null && clip.relevance_score !== undefined
    && Number.isFinite(Number(clip.relevance_score)) ? Number(clip.relevance_score) : null;
  const markerPosition = Number(totalDuration) > 0
    ? Math.max(0, Math.min(100, (Number(scene.start_time) || 0) / Number(totalDuration) * 100))
    : 0;
  return (
    <details className="timeline-scene">
      <summary><span className="scene-number">{String(index + 1).padStart(2, "0")}</span><span className="timeline-track"><i style={{ left: `${markerPosition}%` }} /></span><span className="scene-summary"><strong>{scene.text || "Scene details"}</strong><small>{formatDuration(scene.start_time) || "—"} – {formatDuration(scene.end_time) || "—"}</small></span><ChevronDown size={14} /></summary>
      <div className="scene-details">
        {clip.image_url && <img className="generated-scene-image" src={fileUrl(clip.image_url)} alt={`Generated visual for ${scene.scene_id}`} loading="lazy" />}
        {clip.model && <p><strong>Image model</strong><span>{clip.model}</span></p>}
        {scene.search_query && <p><strong>Search query</strong><span>{scene.search_query}</span></p>}
        {score !== null && <p><strong>Relevance</strong><span>{clip.provider_mode === "demo" ? "Not scene-matched (demo)" : `${Math.round(score * 100)}% keyword overlap`}</span></p>}
        {clip.selection_reason && <p><strong>Selection reason</strong><span>{clip.selection_reason}</span></p>}
        {clip.matching_method && <p><strong>Matching method</strong><span>{clip.matching_method}</span></p>}
        {clip.provider && <p><strong>Source</strong><span>{clip.provider}{clip.creator ? ` · ${clip.creator}` : ""}</span></p>}
        {clip.license_name && <p><strong>Licence</strong><span>{clip.license_url ? <a href={clip.license_url} target="_blank" rel="noreferrer">{clip.license_name}</a> : clip.license_name}</span></p>}
        {clip.attribution_required && <p><strong>Attribution</strong><span>{clip.attribution_required}</span></p>}
        {clip.source_description && <p><strong>Source notes</strong><span>{clip.source_description}</span></p>}
        {clip.source_url && <a className="source-link" href={clip.source_url} target="_blank" rel="noreferrer">View original source <ExternalLink size={12} /></a>}
        {scene.text && <p><strong>Narration / caption</strong><span>{scene.text}</span></p>}
        {clip.fallback_warnings?.map((warning) => <p className="scene-warning" key={warning}><strong>Note</strong><span>{warning}</span></p>)}
      </div>
    </details>
  );
}

function ProcessCard() {
  return <section className="panel process-panel"><div className="panel-heading compact-heading"><div><span className="panel-eyebrow">BEHIND THE CUT</span><h2>From brief to video</h2></div><Sparkles size={17} /></div>
    <div className="process-step"><span>01</span><div><strong>Prepare your story</strong><small>Script, audio narration, or your own scene plan.</small></div></div>
    <div className="process-step"><span>02</span><div><strong>Find the right footage</strong><small>Provider and license details are recorded per scene.</small></div></div>
    <div className="process-step"><span>03</span><div><strong>Render your formats</strong><small>Native MP4 playback, downloads, captions, and timeline.</small></div></div>
  </section>;
}

function CollectionPage({ kind, jobs, loading, error, search, setSearch, titleFor, onCreate, onOpenJob }) {
  const library = kind === "library";
  const items = library ? jobs.flatMap((job) => job.status === "completed" ? (job.result?.videos || []).map((video) => ({ job, video })) : []) : jobs;
  return (
    <div className="page-view collection-page">
      <div className="page-title-row"><div><div className="eyebrow"><FolderKanban size={14} /> WORKSPACE</div><h1>{library ? "Your video library." : "Your projects."}</h1><p>{library ? "Browse generated MP4 outputs that this browser can retrieve." : "Track jobs created from this browser and open their results."}</p></div><button className="primary-button" onClick={onCreate}><Plus size={17} />New video</button></div>
      <div className="collection-toolbar"><label className="search-field"><Search size={16} /><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={library ? "Search videos or project IDs" : "Search projects or job IDs"} aria-label={library ? "Search videos" : "Search projects"} /></label><span className="result-count">{library ? items.length : jobs.length} {library ? "videos" : "projects"}</span></div>
      <div className="history-limit-note"><CircleAlert size={15} /><span>History is limited to job IDs saved in this browser. The backend does not currently provide account-wide project history.</span></div>
      {error && <p className="inline-notice notice-warning"><CircleAlert size={15} />{error}</p>}
      {loading ? <div className="loading-row"><LoaderCircle className="spin" size={17} />Loading locally saved jobs from the backend…</div>
        : library ? items.length ? <div className="library-grid">{items.map(({ job, video }) => <article className="library-card" key={`${job.job_id}-${video.file}`}>
          <div className={`library-video ratio-canvas-${(outputRatio(video) || "16:9").replace(":", "-")}`}>{video.url ? <video controls preload="metadata" src={fileUrl(video.url)} aria-label={`Preview ${titleFor(job)}`} /> : <div className="preview-message"><CircleAlert size={20} /><strong>MP4 link unavailable</strong><p>The backend did not return a video URL for this output.</p></div>}</div>
          <div className="library-card-body"><div className="library-card-title"><div><strong>{titleFor(job)}</strong><small>{formatDate(job.created_at)}</small></div><StatusBadge status={job.status} /></div><div className="video-metadata"><span>{video.width && video.height ? `${video.width} × ${video.height}` : "Resolution unavailable"}</span><i /><span>{formatDuration(video.duration_seconds) || "Duration unavailable"}</span><i /><span>{outputRatio(video) || "Ratio unavailable"}</span></div><div className="library-actions">{video.url ? <a className="secondary-button" href={fileUrl(video.url)} download={video.file}><ArrowDownToLine size={14} />Download MP4</a> : <span className="unavailable-label">MP4 unavailable</span>}<button className="text-button" onClick={() => onOpenJob(job)}>Open timeline <ArrowRight size={14} /></button></div>
            <div className="asset-links">{job.result?.captions_url && <a href={fileUrl(job.result.captions_url)} download><FileText size={13} />captions.srt</a>}{job.result?.timeline_url && <a href={fileUrl(job.result.timeline_url)} target="_blank" rel="noreferrer"><FileText size={13} />timeline.json</a>}{job.result?.manifest_url && <a href={fileUrl(job.result.manifest_url)} download><Download size={13} />manifest.json</a>}</div>
          </div>
        </article>)}</div>
          : <EmptyState icon={MonitorPlay} title={jobs.length ? "No generated videos match" : "No videos available"}>Completed videos appear here when their job IDs are available in this browser. There is no global library endpoint.</EmptyState>
        : jobs.length ? <div className="projects-table">{jobs.map((job) => <ProjectRow key={job.job_id} job={job} title={titleFor(job)} onClick={() => onOpenJob(job)} />)}</div>
          : <EmptyState icon={FolderKanban} title="No projects saved yet">Create your first video to see its job status and outputs here.</EmptyState>}
    </div>
  );
}

function SettingsPage({ readiness, voices, voiceError }) {
  const checks = readiness?.checks || {};
  const provider = checks.footage_provider;
  return <div className="page-view settings-page">
    <div className="page-title-row"><div><div className="eyebrow"><Settings size={14} /> WORKSPACE</div><h1>Studio settings.</h1><p>Connection and capabilities reported by your configured backend.</p></div></div>
    <div className="settings-layout">
      <section className="panel settings-card"><div className="panel-heading"><div><span className="panel-eyebrow">BACKEND CONNECTION</span><h2>Render engine status</h2></div><span className={`status-badge ${readiness?.ready ? "status-completed" : readiness ? "status-failed" : ""}`}><i />{readiness?.ready ? "Ready" : readiness ? "Needs attention" : "Checking"}</span></div>
        <p className="settings-description">Readiness is checked against the backend’s <code>/ready</code> endpoint. These results reflect the current connection.</p>
        <div className="check-list">{["database", "ffmpeg", "ffprobe"].map((name) => <div className="check-row" key={name}><span className={`check-status ${checks[name] === "ready" ? "check-ok" : "check-no"}`}>{checks[name] === "ready" ? <Check size={14} /> : <CircleAlert size={14} />}</span><span><strong>{name === "ffmpeg" ? "FFmpeg renderer" : name === "ffprobe" ? "FFprobe validator" : "Job database"}</strong><small>{checks[name] || "Not reported"}</small></span></div>)}
          <div className="check-row"><span className={`check-status ${provider ? "check-ok" : "check-no"}`}>{provider ? <Check size={14} /> : <CircleAlert size={14} />}</span><span><strong>Footage provider</strong><small>{provider?.selected ? `${provider.selected} · credentials ${provider.credentials || "status not reported"}` : "Not reported"}</small></span></div></div>
        <div className="api-endpoint"><span>API endpoint</span><code>{API_URL}</code><a href={`${API_URL}/docs`} target="_blank" rel="noreferrer" aria-label="Open API docs"><ExternalLink size={14} /></a></div>
      </section>
      <section className="panel settings-card"><div className="panel-heading"><div><span className="panel-eyebrow">LOCAL NARRATION</span><h2>Available voices</h2></div><Mic2 size={17} /></div>
        {voiceError ? <p className="inline-notice notice-warning"><CircleAlert size={14} />{voiceError}</p> : voices.length ? <><p className="settings-description">{voices.length} voice{voices.length === 1 ? "" : "s"} returned by the backend.</p><ul className="voice-list">{voices.map((item) => <li key={item.id}><Mic2 size={14} /><span>{item.name}</span></li>)}</ul></> : <p className="settings-description">No local voices have been reported.</p>}
      </section>
      <section className="panel settings-card settings-limits"><div className="panel-heading"><div><span className="panel-eyebrow">ABOUT THIS WORKSPACE</span><h2>History and capabilities</h2></div><BookOpen size={17} /></div>
        <div className="capability-row"><strong>Project history</strong><span>Browser remembers known job IDs. No account-wide listing endpoint is currently available.</span></div>
        <div className="capability-row"><strong>Job controls</strong><span>Jobs can be created and polled. Editing an existing render or re-rendering is not exposed by the backend.</span></div>
        <div className="capability-row"><strong>Audio transcription</strong><span>Audio-only transcription requires the optional faster-whisper backend dependency; transcript scene notes can be supplied instead.</span></div>
      </section>
    </div>
  </div>;
}

function MediaLibraryPage() {
  const [library, setLibrary] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const refresh = async (action = getMediaLibrary) => {
    setBusy(true); setError("");
    try { setLibrary(await action()); } catch (e) { setError(getErrorMessage(e, "Media library request failed.")); }
    finally { setBusy(false); }
  };
  useEffect(() => { refresh(); }, []);
  const items = (library?.items || []).filter((item) => `${item.category} ${item.caption} ${(item.tags || []).join(" ")} ${item.provider}`.toLowerCase().includes(filter.toLowerCase()));
  return <div className="page-view collection-page">
    <div className="page-title-row"><div><div className="eyebrow"><Film size={14} /> LOCAL FOOTAGE</div><h1>Media library.</h1><p>Locally catalogued footage with recorded source and licensing metadata.</p></div>
      <div className="page-actions"><button className="secondary-button" disabled={busy} onClick={() => refresh(() => downloadMediaDataset(true, 20))}><Search size={15} />Preview collection</button><button className="secondary-button" disabled={busy} onClick={() => refresh(scanMediaLibrary)}><Activity size={15} />Scan and validate</button><button className="primary-button" disabled={busy} onClick={() => refresh(() => downloadMediaDataset(false, 20))}><Download size={15} />Download dataset</button></div></div>
    <div className="collection-toolbar"><label className="search-field"><Search size={16} /><input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search captions, tags, categories" aria-label="Search media" /></label><span className="result-count">{library?.valid_videos ?? "—"} videos · {library?.valid_images ?? "—"} images · {library ? `${(library.storage_bytes / 1048576).toFixed(1)} MB` : "—"}</span></div>
    {error && <p className="inline-notice notice-warning"><CircleAlert size={14} />{error}</p>}
    {library?.source_availability?.pexels === "missing API key" && <p className="inline-notice notice-warning"><CircleAlert size={14} />Pexels dataset download is unavailable because PEXELS_API_KEY is not configured. Existing local media and demo rendering remain available.</p>}
    {library?.failures?.map((failure) => <p className="inline-notice notice-warning" key={failure}><CircleAlert size={14} />{failure}</p>)}
    {busy && <div className="loading-row"><LoaderCircle className="spin" size={17} />Backend operation in progress…</div>}
    <div className="check-list">{(library?.categories || []).map((category) => <div className="capability-row" key={category}><strong>{category}</strong><span>{(library?.items || []).filter((item) => item.category === category).length} items</span></div>)}</div>
    <div className="library-grid">{items.map((item) => <article className="library-card" key={item.id}><div className="library-video">{item.media_type === "image" ? <img src={`${API_URL}/api/v1/media/${encodeURIComponent(item.id)}/file`} alt={item.caption} loading="lazy" /> : <video muted controls preload="metadata" src={`${API_URL}/api/v1/media/${encodeURIComponent(item.id)}/file`} aria-label={item.caption} />}</div><div className="library-card-body"><div className="panel-eyebrow">{item.category} · {item.media_type}</div><strong>{item.caption}</strong><p>{(item.tags || []).join(", ")}</p><small>{item.provider} {item.creator ? `· ${item.creator}` : ""}</small><div className="asset-links">{item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Source <ExternalLink size={12} /></a>}{item.license_url && <a href={item.license_url} target="_blank" rel="noreferrer">{item.license_name} <ExternalLink size={12} /></a>}{item.attribution_required && <span>{item.attribution_required}</span>}</div></div></article>)}</div>
    {!items.length && !busy && <EmptyState icon={Film} title="No catalogued media yet">Scan local files, or configure PEXELS_API_KEY and download a bounded dataset.</EmptyState>}
  </div>;
}

export default App;
