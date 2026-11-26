(() => {
  'use strict';

  const API = window.JOBHUNTER_API_BASE || 'http://127.0.0.1:8768/api/v1/jobhunter';
  const LOCAL_KEY = 'jobhunter.demo.tracker.v1';
  const STAGES = [
    ['unread', 'Not read'], ['read', 'Reading'], ['generated', 'Generating application'],
    ['checked', 'Control application'], ['sent', 'Sending out'], ['confirmed', 'Confirming'],
    ['waiting', 'Waiting'], ['contact', 'Call / contact'], ['interview', 'Interview'],
    ['preparation', 'Interview preparation'], ['awaiting', 'Waiting for outcome'],
    ['success', 'Success'], ['rejected', 'Fail'], ['blocked', 'Blocked'],
    ['unclear', 'Unclear'], ['skipped', 'Skipped']
  ];
  const STATUS_OF_STAGE = {
    unread: 'new', read: 'new', generated: 'new', checked: 'new',
    sent: 'applied', confirmed: 'applied', waiting: 'applied', contact: 'applied',
    interview: 'interview', preparation: 'interview', awaiting: 'interview',
    success: 'success', rejected: 'rejected', blocked: 'blocked', unclear: 'unclear', skipped: 'skipped'
  };
  const STATUS_LABELS = [
    ['new', 'New'], ['applied', 'Applied'], ['interview', 'Interview'],
    ['success', 'Success'], ['rejected', 'Fail'], ['blocked', 'Blocked'],
    ['unclear', 'Unclear'], ['skipped', 'Skipped']
  ];
  const EXPECTED_HOSTS = new Set(['jobs.ams.at', 'www.karriere.at', 'www.jobs.at']);
  const $ = (selector) => document.querySelector(selector);
  const loginView = $('#login-view');
  const appView = $('#app-view');
  const loginForm = $('#login-form');
  const loginStatus = $('#login-status');
  const scanStatus = $('#scan-status');
  const profileSelect = $('#profile-select');
  const portalList = $('#portal-list');
  const results = $('#results');
  let profiles = [];
  let portals = [];
  let latestResults = [];
  let selectedPacketId = '';

  function emptyState() {
    return { jobs: {}, packets: {}, profileId: 'operations-demo', selectedPortals: ['ams'], latestResults: [], lastScanAt: '', activeView: 'listings' };
  }

  function readLocal() {
    try {
      const value = JSON.parse(localStorage.getItem(LOCAL_KEY) || '{}');
      if (!value || typeof value !== 'object' || Array.isArray(value)) return emptyState();
      return { ...emptyState(), ...value,
        jobs: value.jobs && typeof value.jobs === 'object' ? value.jobs : {},
        packets: value.packets && typeof value.packets === 'object' ? value.packets : {},
        latestResults: Array.isArray(value.latestResults) ? value.latestResults : [] };
    } catch (_) { return emptyState(); }
  }

  function writeLocal(state) {
    try { localStorage.setItem(LOCAL_KEY, JSON.stringify(state)); }
    catch (_) { setStatus(scanStatus, 'Browser storage is unavailable. Changes will not persist.', true); }
  }

  function setStatus(node, text, isError = false) {
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('error', Boolean(isError));
  }

  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      ...options,
      mode: 'cors',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      const error = new Error(payload.detail || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function notifyVisit() {
    try {
      fetch(`${API}/visit`, {
        method: 'POST', mode: 'cors',
        headers: { 'Content-Type': 'application/json' },
        body: '{}', keepalive: true
      }).catch(() => {});
    } catch (_) { /* a notification must never block the page */ }
  }

  function showLogin(message = '') {
    loginView.hidden = false;
    appView.hidden = true;
    $('#logout-button').hidden = true;
    setStatus(loginStatus, message, Boolean(message));
  }

  function showApp() {
    loginView.hidden = true;
    appView.hidden = false;
    $('#logout-button').hidden = false;
  }

  async function loadDemo() {
    const [profileData, portalData] = await Promise.all([request('/profiles'), request('/portals')]);
    profiles = profileData.profiles;
    portals = portalData.portals;
    latestResults = readLocal().latestResults;
    renderSelectors();
    renderWarnings();
    renderAllViews();
    showApp();
    showView(readLocal().activeView || 'listings');
  }

  function renderSelectors() {
    const saved = readLocal();
    profileSelect.replaceChildren();
    profiles.forEach((profile) => {
      const option = document.createElement('option');
      option.value = profile.id;
      option.textContent = profile.label;
      profileSelect.append(option);
    });
    const selected = profiles.some((item) => item.id === saved.profileId) ? saved.profileId : 'operations-demo';
    if (profiles.some((item) => item.id === selected)) profileSelect.value = selected;
    updateProfileSummary();

    portalList.replaceChildren();
    const checkedPortals = Array.isArray(saved.selectedPortals) ? saved.selectedPortals : ['ams'];
    portals.forEach((portal) => {
      const label = create('label', 'portal-option');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'portal';
      input.value = portal.id;
      input.checked = checkedPortals.includes(portal.id) || (!saved.selectedPortals && portal.id === 'ams');
      const copy = create('span', 'portal-copy');
      copy.append(create('strong', '', portal.label));
      copy.append(create('small', '', portal.live_available ? 'Live adapter available' : 'Synthetic demo only'));
      const marker = create('span', 'checkmark');
      marker.setAttribute('aria-hidden', 'true');
      label.append(input, copy, marker);
      portalList.append(label);
    });

    profileSelect.onchange = () => {
      const state = readLocal();
      state.profileId = profileSelect.value;
      writeLocal(state);
      updateProfileSummary();
      renderProfile();
    };
    portalList.onchange = () => {
      const state = readLocal();
      state.selectedPortals = selectedPortals();
      writeLocal(state);
    };
  }

  function updateProfileSummary() {
    const profile = profiles.find((item) => item.id === profileSelect.value);
    $('#profile-summary').textContent = profile ? profile.summary : '';
  }

  function selectedPortals() {
    return [...portalList.querySelectorAll('input[name="portal"]:checked')].map((input) => input.value);
  }

  function create(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function trackerFor(jobId) {
    const all = readLocal();
    if (!all.jobs[jobId] || typeof all.jobs[jobId] !== 'object') {
      all.jobs[jobId] = { stage: 'unread', bookmarked: false, note: '', history: [] };
    }
    const item = all.jobs[jobId];
    if (!Array.isArray(item.history)) item.history = [];
    if (typeof item.bookmarked !== 'boolean') item.bookmarked = false;
    if (typeof item.note !== 'string') item.note = '';
    if (!STAGES.some(([value]) => value === item.stage)) item.stage = 'unread';
    return item;
  }

  function persistJob(jobId, item) {
    const all = readLocal();
    all.jobs[jobId] = item;
    writeLocal(all);
    renderOverview();
    renderTracker();
  }

  function stageLabel(stage) {
    return (STAGES.find(([value]) => value === stage) || [stage, stage])[1];
  }

  function setStage(jobId, next) {
    const item = trackerFor(jobId);
    if (item.stage === next || !STAGES.some(([value]) => value === next)) return;
    item.history.push({ stage: item.stage, changedAt: new Date().toISOString() });
    item.stage = next;
    persistJob(jobId, item);
  }

  function undoStage(jobId) {
    const item = trackerFor(jobId);
    const previous = item.history.pop();
    if (!previous || !STAGES.some(([value]) => value === previous.stage)) return;
    item.stage = previous.stage;
    persistJob(jobId, item);
    renderListings();
  }

  function makeDraft(job) {
    const profile = profiles.find((item) => item.id === (profileSelect.value || readLocal().profileId));
    const keywords = (profile && profile.keywords || []).slice(0, 4).join(', ') || 'sample criteria';
    return `Dear Hiring Team,\n\nI am interested in the ${job.title} position at ${job.company}. This fictional demo draft uses the ${profile ? profile.label : 'sample'} profile.\n\nThe sample criteria emphasize ${keywords}. I would welcome a conversation about how these strengths could support your team.\n\nKind regards,\nSample Candidate\n\nFictional demo draft. Replace every detail before real use.`;
  }

  function generatePacket(job) {
    const state = readLocal();
    const packet = state.packets[job.id] || {
      id: job.id, title: job.title, company: job.company, profileId: profileSelect.value,
      currentVersion: 0, versions: []
    };
    packet.title = job.title;
    packet.company = job.company;
    packet.profileId = profileSelect.value;
    packet.versions.push({ createdAt: new Date().toISOString(), source: 'generated-demo-template', text: makeDraft(job) });
    packet.currentVersion = packet.versions.length - 1;
    state.packets[job.id] = packet;
    writeLocal(state);
    selectedPacketId = job.id;
    renderReview();
    renderOverview();
    showView('review');
  }

  function openPortalHome(job) {
    try {
      const url = new URL(job.source_url);
      if (url.protocol === 'https:' && EXPECTED_HOSTS.has(url.hostname)) window.open(url.href, '_blank', 'noopener,noreferrer');
    } catch (_) { /* reject unknown destinations */ }
  }

  function renderJob(job) {
    const card = create('article', 'job-card');
    const item = trackerFor(job.id);
    const top = create('div', 'job-top');
    top.append(create('h3', '', job.title), create('span', 'score-pill', `${job.score}% match`));
    const meta = create('p', 'job-meta', `${job.company} · ${job.location} · posted ${new Date(job.posted).toLocaleDateString()}`);
    const description = create('p', 'job-description', job.description);
    const sample = create('p', 'sample-label', 'Fictional sample listing - not a current vacancy');
    const keywords = create('div', 'keyword-list');
    (job.matched_keywords || []).forEach((word) => keywords.append(create('span', 'keyword', word)));

    const actions = create('div', 'job-actions');
    const stageWrap = create('label', 'stage-label', 'Application stage');
    const stage = document.createElement('select');
    stage.className = 'stage-select';
    stage.setAttribute('aria-label', `Tracking stage for ${job.title}`);
    STAGES.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      stage.append(option);
    });
    stage.value = item.stage;
    stageWrap.append(stage);
    const bookmark = create('button', 'button tiny secondary', item.bookmarked ? '★ Saved' : '☆ Save');
    bookmark.type = 'button';
    bookmark.setAttribute('aria-pressed', String(item.bookmarked));
    const undo = create('button', 'button tiny secondary', 'Undo stage');
    undo.type = 'button';
    undo.disabled = !item.history.length;
    actions.append(stageWrap, bookmark, undo);

    const noteLabel = create('label', 'note-label', 'Private browser note');
    const note = document.createElement('textarea');
    note.rows = 2;
    note.maxLength = 1000;
    note.placeholder = 'Optional note saved only in this browser';
    note.value = item.note;
    noteLabel.append(note);

    const packetActions = create('div', 'packet-actions');
    const draftButton = create('button', 'button tiny primary', 'Generate demo application');
    draftButton.type = 'button';
    const savedPacket = readLocal().packets[job.id];
    const reviewButton = create('button', 'button tiny secondary', savedPacket ? 'Review saved draft' : 'No draft yet');
    reviewButton.type = 'button';
    reviewButton.disabled = !savedPacket;
    packetActions.append(draftButton, reviewButton);

    const footer = create('div', 'card-footer');
    const source = create('button', 'source-link source-button', `Open ${job.portal_label || 'portal'} home`);
    source.type = 'button';
    footer.append(source, create('span', 'fixture-note', 'Local HTML fixture'));

    stage.addEventListener('change', () => { setStage(job.id, stage.value); undo.disabled = !trackerFor(job.id).history.length; });
    bookmark.addEventListener('click', () => {
      const current = trackerFor(job.id);
      current.bookmarked = !current.bookmarked;
      persistJob(job.id, current);
      bookmark.textContent = current.bookmarked ? '★ Saved' : '☆ Save';
      bookmark.setAttribute('aria-pressed', String(current.bookmarked));
    });
    undo.addEventListener('click', () => { undoStage(job.id); });
    note.addEventListener('input', () => { const current = trackerFor(job.id); current.note = note.value; persistJob(job.id, current); });
    draftButton.addEventListener('click', () => generatePacket(job));
    reviewButton.addEventListener('click', () => { selectedPacketId = job.id; renderReview(); showView('review'); });
    source.addEventListener('click', () => openPortalHome(job));

    card.append(top, meta, description, sample, keywords, actions, noteLabel, packetActions, footer);
    return card;
  }

  function filteredJobs() {
    const search = ($('#search-input').value || '').trim().toLocaleLowerCase();
    const wanted = $('#status-filter').value;
    const sort = $('#sort-select').value;
    const all = latestResults.flatMap((result) => (result.jobs || []).map((job) => ({ ...job, portal: result.portal, portal_label: result.portal_label })));
    const filtered = all.filter((job) => {
      const haystack = `${job.title} ${job.company} ${job.location} ${job.description}`.toLocaleLowerCase();
      const stage = trackerFor(job.id).stage;
      return (!search || haystack.includes(search)) && (wanted === 'all' || STATUS_OF_STAGE[stage] === wanted);
    });
    filtered.sort((a, b) => sort === 'match' ? b.score - a.score || b.posted.localeCompare(a.posted) : b.posted.localeCompare(a.posted));
    return filtered;
  }

  function renderListings() {
    results.replaceChildren();
    if (!latestResults.length) {
      results.append(create('p', 'empty-state', 'Choose a profile and at least one portal, then run a demo scan.'));
      return;
    }
    results.append(create('p', 'results-notice', 'Synthetic fixture results only. No job portal was contacted.'));
    const jobs = filteredJobs();
    if (!jobs.length) {
      results.append(create('p', 'empty-state', 'No sample listing matches these filters.'));
      return;
    }
    latestResults.forEach((portalResult) => {
      const portalJobs = jobs.filter((job) => job.portal === portalResult.portal);
      if (!portalJobs.length) return;
      const section = create('section', 'portal-results');
      const heading = create('div', 'results-heading');
      heading.append(create('div', 'results-heading-copy', `${portalResult.portal_label} · ${portalJobs.length} newest matches`));
      heading.append(create('span', 'mode-pill', 'synthetic data'));
      const cards = create('div', 'job-grid');
      portalJobs.forEach((job) => cards.append(renderJob(job)));
      section.append(heading, cards, create('p', 'disabled-reason', portalResult.live_reason));
      results.append(section);
    });
  }

  function allCurrentJobs() {
    return latestResults.flatMap((result) => result.jobs || []);
  }

  function renderOverview() {
    const jobs = allCurrentJobs();
    const counts = Object.fromEntries(STATUS_LABELS.map(([key]) => [key, 0]));
    let bookmarked = 0;
    jobs.forEach((job) => {
      const item = trackerFor(job.id);
      counts[STATUS_OF_STAGE[item.stage]] = (counts[STATUS_OF_STAGE[item.stage]] || 0) + 1;
      if (item.bookmarked) bookmarked += 1;
    });
    const stats = $('#overview-stats');
    if (!stats) return;
    stats.replaceChildren();
    [['Listings', jobs.length], ['New', counts.new], ['Applied', counts.applied], ['Interview', counts.interview], ['Saved', bookmarked]].forEach(([label, value]) => {
      const card = create('div', 'stat-card');
      card.append(create('span', 'stat-label', label), create('strong', 'stat-value', String(value)));
      stats.append(card);
    });
    const saved = readLocal();
    $('#last-scan-summary').textContent = saved.lastScanAt
      ? `${new Date(saved.lastScanAt).toLocaleString()} · ${jobs.length} sample listings across ${latestResults.length} selected portals.`
      : 'No scan has been run in this browser yet.';
  }

  function renderTracker() {
    const target = $('#tracker-groups');
    if (!target) return;
    target.replaceChildren();
    const jobs = allCurrentJobs();
    if (!jobs.length) { target.append(create('p', 'empty-state', 'Run a demo scan to populate the tracker.')); return; }
    STATUS_LABELS.forEach(([status, label]) => {
      const groupJobs = jobs.filter((job) => STATUS_OF_STAGE[trackerFor(job.id).stage] === status);
      const group = create('section', 'tracker-group');
      const heading = create('h3', '', `${label} (${groupJobs.length})`);
      const list = create('div', 'tracker-list');
      groupJobs.forEach((job) => {
        const row = create('div', 'tracker-row');
        row.append(create('div', 'tracker-job', job.title), create('span', 'tracker-company', job.company));
        row.append(create('span', 'tracker-stage', stageLabel(trackerFor(job.id).stage)));
        list.append(row);
      });
      group.append(heading, list);
      target.append(group);
    });
  }

  function renderProfile() {
    const profile = profiles.find((item) => item.id === profileSelect.value) || profiles[0];
    if (!profile) return;
    $('#profile-view-title').textContent = profile.label;
    $('#profile-view-summary').textContent = profile.summary;
    const keywords = $('#profile-keywords');
    keywords.replaceChildren();
    (profile.keywords || []).forEach((word) => keywords.append(create('span', 'keyword', word)));
  }

  function renderWarnings() {
    const list = $('#portal-warnings');
    list.replaceChildren();
    portals.forEach((portal) => {
      const item = create('div', 'warning-item');
      item.append(create('strong', '', portal.label));
      item.append(create('p', 'help-text', portal.live_available ? 'Live adapter available.' : portal.live_reason));
      list.append(item);
    });
  }

  function renderReview() {
    const state = readLocal();
    const list = $('#packet-list');
    const editor = $('#packet-editor');
    if (!list || !editor) return;
    list.replaceChildren();
    const packets = Object.values(state.packets).filter((packet) => packet && Array.isArray(packet.versions) && packet.versions.length);
    if (!packets.some((packet) => packet.id === selectedPacketId)) selectedPacketId = packets[0] ? packets[0].id : '';
    packets.forEach((packet) => {
      const button = create('button', `packet-choice${packet.id === selectedPacketId ? ' is-selected' : ''}`, `${packet.title} · ${packet.company}`);
      button.type = 'button';
      button.addEventListener('click', () => { selectedPacketId = packet.id; renderReview(); });
      list.append(button);
    });
    editor.replaceChildren();
    const packet = packets.find((item) => item.id === selectedPacketId);
    if (!packet) { editor.append(create('p', 'empty-state', 'Generate a demo draft from a listing to begin.')); return; }
    const index = Math.min(Math.max(Number(packet.currentVersion) || 0, 0), packet.versions.length - 1);
    const version = packet.versions[index];
    editor.append(create('p', 'eyebrow', `${packet.title} · ${packet.company}`));
    editor.append(create('p', 'help-text', `Version ${index + 1} of ${packet.versions.length} · ${new Date(version.createdAt).toLocaleString()} · ${version.source === 'user-edit' ? 'saved revision' : 'fictional template'}`));
    const textarea = document.createElement('textarea');
    textarea.className = 'draft-editor';
    textarea.setAttribute('aria-label', 'Fictional application draft');
    textarea.value = version.text;
    const controls = create('div', 'action-row');
    const save = create('button', 'button primary', 'Save revision');
    const copy = create('button', 'button secondary', 'Copy text');
    const download = create('button', 'button secondary', 'Download draft');
    const previous = create('button', 'button quiet', 'Restore previous version');
    const message = create('span', 'status', '');
    save.type = copy.type = download.type = previous.type = 'button';
    previous.disabled = packet.versions.length < 2;
    save.addEventListener('click', () => {
      const fresh = readLocal();
      const target = fresh.packets[packet.id];
      target.versions.push({ createdAt: new Date().toISOString(), source: 'user-edit', text: textarea.value });
      target.currentVersion = target.versions.length - 1;
      writeLocal(fresh);
      setStatus(message, 'Revision saved in this browser.');
      renderReview();
      renderOverview();
    });
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(textarea.value); setStatus(message, 'Draft copied.'); }
      catch (_) { setStatus(message, 'Clipboard access is unavailable in this browser.', true); }
    });
    download.addEventListener('click', () => downloadText(`${packet.id}-demo-draft.txt`, textarea.value));
    previous.addEventListener('click', () => {
      const fresh = readLocal();
      const target = fresh.packets[packet.id];
      target.currentVersion = Math.max(0, target.versions.length - 2);
      writeLocal(fresh);
      renderReview();
    });
    controls.append(save, copy, download, previous, message);
    editor.append(textarea, controls);
  }

  function renderAllViews() {
    renderListings();
    renderOverview();
    renderTracker();
    renderProfile();
    renderReview();
  }

  function showView(name) {
    const allowed = ['listings', 'overview', 'review', 'profile', 'tracker', 'system'];
    const view = allowed.includes(name) ? name : 'listings';
    document.querySelectorAll('.app-view').forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll('.tab').forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    const state = readLocal();
    state.activeView = view;
    writeLocal(state);
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function exportTracker() {
    const data = readLocal();
    const payload = {
      exported_at: new Date().toISOString(),
      privacy: 'Local browser demo data. This export may contain notes and draft text you entered.',
      profile_id: data.profileId || profileSelect.value,
      selected_portals: data.selectedPortals,
      jobs: data.jobs,
      packets: data.packets,
      latest_results: data.latestResults
    };
    downloadText('jobhunter-demo-tracker.json', JSON.stringify(payload, null, 2));
  }

  async function runScan(statusNode = scanStatus) {
    const portalIds = selectedPortals();
    if (!portalIds.length) { setStatus(statusNode, 'Select at least one portal.', true); return; }
    const button = $('#scan-button');
    if (button) button.disabled = true;
    setStatus(statusNode, 'Parsing local sample fixtures…');
    try {
      const payload = await request('/scan', { method: 'POST', body: JSON.stringify({ portals: portalIds, profile_id: profileSelect.value }) });
      latestResults = payload.results;
      const state = readLocal();
      state.latestResults = latestResults;
      state.lastScanAt = new Date().toISOString();
      state.profileId = profileSelect.value;
      state.selectedPortals = portalIds;
      writeLocal(state);
      renderAllViews();
      setStatus(statusNode, 'Demo listings ready. No portal was contacted.');
    } catch (error) {
      if (error.status === 401) showLogin('Your demo session expired. Enter the access code again.');
      else setStatus(statusNode, error.message || 'Could not load sample listings.', true);
    } finally { if (button) button.disabled = false; }
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = $('#passcode');
    const passcode = input.value;
    input.value = '';
    setStatus(loginStatus, 'Checking access code…');
    try {
      await request('/login', { method: 'POST', body: JSON.stringify({ passcode }) });
      await loadDemo();
      setStatus(loginStatus, '');
    } catch (error) { showLogin(error.message || 'Could not unlock the demo.'); }
  });

  $('#scan-button').addEventListener('click', () => runScan(scanStatus));
  $('#refresh-button').addEventListener('click', () => runScan($('#system-status')));
  $('#export-button').addEventListener('click', exportTracker);
  $('#system-export-button').addEventListener('click', exportTracker);
  $('#search-input').addEventListener('input', renderListings);
  $('#status-filter').addEventListener('change', renderListings);
  $('#sort-select').addEventListener('change', renderListings);
  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => showView(tab.dataset.view)));
  document.querySelectorAll('[data-go-view]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.goView)));

  $('#logout-button').addEventListener('click', async () => {
    try { await request('/logout', { method: 'POST', body: '{}' }); } catch (_) { /* lock locally anyway */ }
    showLogin('Demo locked.');
  });

  STATUS_LABELS.forEach(([value, label]) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    $('#status-filter').append(option);
  });
  document.addEventListener('keydown', (event) => {
    if (appView.hidden || event.ctrlKey || event.metaKey || event.altKey) return;
    const isTyping = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (event.key === '/' && !isTyping) {
      event.preventDefault(); showView('listings'); $('#search-input').focus();
    } else if (event.key === '?' && !isTyping) { showView('system'); }
  });

  notifyVisit();
  loadDemo().catch((error) => {
    if (error.status === 401) showLogin();
    else showLogin('Demo service is unavailable. Please try again later.');
  });
})();
