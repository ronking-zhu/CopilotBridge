(() => {
  'use strict';

  let v23PreserveInitialView = initialParams.get('knowledge') === '1'
    || initialParams.get('dashboard') === '1';
  const v23T = (zh, en) => window.cbUiText ? window.cbUiText(zh, en) : zh;

  const v23State = {
    view: 'conversation',
    navigationRevision: 0,
    sessionFilter: 'all',
    sessionQuery: '',
    inboxFilter: 'attention',
    selectedInboxId: '',
    detailRequest: 0,
    organizeSession: null,
     knowledgeQuery: '',
     knowledgeStatus: '',
     knowledgeItems: [],
     selectedKnowledgeId: '',
    knowledgeListRequest: 0,
     knowledgeDetailRequest: 0,
     knowledgeSource: null,
    knowledgeMode: 'map',
    knowledgeMap: null,
    knowledgeMapEligibleCount: 0,
    knowledgeMapRequest: 0,
    knowledgeMapQuery: '',
    knowledgeMapScale: 1,
    selectedMapNodeId: '',
    collapsedMapNodes: new Set(),
    knowledgeJobs: [],
    knowledgeSubmissions: new Set(),
    knowledgeJobsRequest: 0,
    displayedKnowledgeJobId: '',
  };

  const v23Icons = {
    dashboard: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    sessions: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
    conversation: '<svg viewBox="0 0 24 24"><path d="M4 4h16v13H8l-4 3z"/><path d="M8 8h8M8 12h5"/></svg>',
    knowledge: '<svg viewBox="0 0 24 24"><path d="M3 5.5A3.5 3.5 0 0 1 6.5 2H11v17H6.5A3.5 3.5 0 0 0 3 22z"/><path d="M21 5.5A3.5 3.5 0 0 0 17.5 2H13v17h4.5A3.5 3.5 0 0 1 21 22z"/><path d="M6.5 7H9M15 7h2.5"/></svg>',
    knowledgeGenerate: '<svg viewBox="0 0 24 24"><path d="M3 5.5A3.5 3.5 0 0 1 6.5 2H11v17H6.5A3.5 3.5 0 0 0 3 22z"/><path d="M13 19h4.5a3.5 3.5 0 0 1 3.5 3V10"/><path d="m18 2 .6 1.4L20 4l-1.4.6L18 6l-.6-1.4L16 4l1.4-.6zM21 6l.4.9.9.4-.9.4-.4.9-.4-.9-.9-.4.9-.4z"/></svg>',
    settings: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a8 8 0 0 0-1.7-1L14.5 3h-5L9 6.1a8 8 0 0 0-1.7 1l-2.4-1-2 3.4L5 11a7 7 0 0 0 0 2l-2.1 1.5 2 3.4 2.4-1a8 8 0 0 0 1.7 1l.5 3.1h5l.5-3.1a8 8 0 0 0 1.7-1l2.4 1 2-3.4L19 13a7 7 0 0 0 .1-1z"/></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/></svg>',
  };

  function v23CreateNav(view, label, icon) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'v23-nav';
    button.dataset.view = view;
    button.title = label;
    button.setAttribute('aria-label', label);
    button.innerHTML = v23Icons[icon];
    button.onclick = () => v23ShowView(view);
    return button;
  }

  const v23Rail = document.createElement('nav');
  v23Rail.id = 'productRail';
  v23Rail.setAttribute('aria-label', v23T('产品导航', 'Product navigation'));
  const v23Brand = document.createElement('div');
  v23Brand.className = 'v23-brand';
  v23Brand.textContent = '>_';
  const v23InboxNav = v23CreateNav('inbox', 'Dashboard', 'dashboard');
  const v23NavBadge = document.createElement('span');
  v23NavBadge.className = 'v23-nav-badge';
  v23NavBadge.hidden = true;
  v23InboxNav.appendChild(v23NavBadge);
  v23Rail.append(
    v23Brand,
    v23InboxNav,
    v23CreateNav('sessions', v23T('会话历史', 'History'), 'sessions'),
    v23CreateNav('conversation', v23T('会话内容', 'Conversation'), 'conversation'),
     v23CreateNav('knowledge', v23T('知识库', 'Knowledge'), 'knowledge'),
  );
  const v23Spacer = document.createElement('div');
  v23Spacer.className = 'v23-nav-spacer';
  v23Rail.append(v23Spacer, v23CreateNav(
    'settings', v23T('同步与设置', 'Sync and settings'), 'settings'
  ));
  document.body.prepend(v23Rail);

  const v23Header = document.querySelector('header');
  const v23Search = document.createElement('div');
  v23Search.id = 'v23GlobalSearch';
  v23Search.innerHTML = v23Icons.search
    + '<input type="search" placeholder="'
    + v23T('搜索会话、设备、项目或标签', 'Search conversations, devices, projects, or labels')
    + '" aria-label="' + v23T('搜索会话', 'Search conversations') + '">'
    + '<kbd>Ctrl K</kbd>';
  const v23SyncIndicator = document.createElement('div');
  v23SyncIndicator.id = 'v23SyncIndicator';
  v23SyncIndicator.textContent = v23T('本地', 'Local');
  v23Header.insertBefore(v23Search, document.querySelector('#inboxBtn'));
  v23Header.insertBefore(v23SyncIndicator, document.querySelector('#timelineBtn'));

  const v23Conversation = document.createElement('main');
  v23Conversation.id = 'conversationView';
  log.parentNode.insertBefore(v23Conversation, log);
  v23Conversation.append(log, form);

  const v29DashboardHead = inboxDrawer.querySelector('.ihead');
  const v29DashboardControls = document.createElement('section');
  v29DashboardControls.id = 'v29DashboardControls';
  v29DashboardControls.setAttribute('aria-label', v23T('通知与同步', 'Notifications and sync'));
  const v29NotificationControls = document.createElement('section');
  v29NotificationControls.className = 'v29-notification-controls';
  const v29NotificationHeading = document.createElement('h3');
  v29NotificationHeading.textContent = v23T('回复通知', 'Response notifications');
  v29NotificationControls.append(
    v29NotificationHeading,
    document.querySelector('#notificationSetupNotice'),
    inboxDrawer.querySelector('.dashactions'),
  );
  inboxDrawer.insertBefore(v29DashboardControls, inboxList);
  v29DashboardControls.append(v29NotificationControls, document.querySelector('#syncSetupPanel'));
  const v29DashboardToggle = document.createElement('button');
  v29DashboardToggle.id = 'v29DashboardToggle';
  v29DashboardToggle.type = 'button';
  v29DashboardToggle.className = 'v23-icon-btn';
  v29DashboardToggle.innerHTML = v23Icons.settings;
  v29DashboardToggle.setAttribute('aria-controls', v29DashboardControls.id);
  v29DashboardHead.insertBefore(inboxDrawer.querySelector('.dashstats'), document.querySelector('#inboxClose'));
  v29DashboardHead.insertBefore(v29DashboardToggle, document.querySelector('#inboxClose'));

  function v29SetDashboardExpanded(expanded) {
    v29DashboardControls.hidden = !expanded;
    v29DashboardToggle.setAttribute('aria-expanded', String(expanded));
    v29DashboardToggle.title = expanded
      ? v23T('收起通知与同步', 'Hide notifications and sync')
      : v23T('显示通知与同步', 'Show notifications and sync');
    v29DashboardToggle.setAttribute('aria-label', v29DashboardToggle.title);
  }
  let v29DashboardExpanded = false;
  try { v29DashboardExpanded = localStorage.getItem('cb_dashboard_expanded') === 'true'; } catch (_) { }
  v29SetDashboardExpanded(v29DashboardExpanded);
  v29DashboardToggle.onclick = () => {
    const expanded = v29DashboardControls.hidden;
    v29SetDashboardExpanded(expanded);
    try { localStorage.setItem('cb_dashboard_expanded', String(expanded)); } catch (_) { }
  };

  const v23InboxFilters = document.createElement('div');
  v23InboxFilters.id = 'v23InboxFilters';
  const v23InboxWorkspace = document.createElement('div');
  v23InboxWorkspace.id = 'v23InboxWorkspace';
  const v23InboxDetail = document.createElement('section');
  v23InboxDetail.id = 'v23InboxDetail';
  v23InboxDetail.innerHTML = '<div class="v23-empty">'
    + v23T('选择一条消息查看详情', 'Select a message to view details') + '</div>';
  inboxList.parentNode.insertBefore(v23InboxFilters, inboxList);
  inboxList.parentNode.insertBefore(v23InboxWorkspace, inboxList);
  v23InboxWorkspace.append(inboxList, v23InboxDetail);

  const v23Settings = document.createElement('section');
  v23Settings.id = 'productSettings';
  v23Settings.setAttribute('aria-label', v23T('同步与设置', 'Sync and settings'));
  v23Settings.innerHTML = '<div class="v23-view-head"><h2>'
    + v23T('同步与设置', 'Sync and settings') + '</h2>'
    + '<button type="button" class="v23-secondary" id="v23ConnectionSettings">'
    + v23T('连接设置', 'Connection settings') + '</button></div>'
    + '<div class="v23-settings-grid" id="v23SettingsGrid"></div>';
  document.body.appendChild(v23Settings);

   const v24Knowledge = document.createElement('section');
   v24Knowledge.id = 'productKnowledge';
   v24Knowledge.setAttribute('aria-label', 'Knowledge Hub');
   v24Knowledge.innerHTML = '<div class="v23-view-head"><h2>Knowledge Hub</h2>'
    + '<div class="v24-head-actions"><button type="button" class="v23-primary" '
    + 'id="v26GenerateMap">' + v23T('整理历史', 'Organize history') + '</button>'
    + '<button type="button" class="v23-secondary" id="v25ExtractKnowledge">'
    + v23T('提炼当前', 'Extract current') + '</button>'
    + '<button type="button" class="v23-secondary" id="v24NewKnowledge">'
    + v23T('手动保存', 'Save manually') + '</button></div></div>'
     + '<div class="v26-knowledge-viewbar"><div class="v26-mode-switch" role="tablist" '
     + 'aria-label="' + v23T('知识视图', 'Knowledge views') + '"><button type="button" id="v26MapTab" role="tab">'
     + v23T('思维导图', 'Mind map') + '</button>'
     + '<button type="button" id="v26ListTab" role="tab">'
     + v23T('知识条目', 'Knowledge items') + '</button></div></div>'
    + '<section class="v28-knowledge-jobs" id="v28KnowledgeJobs" hidden>'
    + '<div class="v28-jobs-head"><strong>' + v23T('生成任务', 'Generation tasks')
    + '</strong><span>' + v23T('任务在后台运行，可安全离开此页面', 'Tasks run in the background; you may safely leave this page') + '</span></div>'
    + '<div id="v28KnowledgeJobList"></div></section>'
     + '<div class="v26-map-workspace" id="v26KnowledgeMap">'
     + '<section class="v26-map-stage"><div class="v26-map-toolbar">'
     + '<div id="v26MapCoverage">' + v23T('尚未生成', 'Not generated') + '</div><input id="v26MapSearch" type="search" '
     + 'placeholder="' + v23T('搜索图谱节点', 'Search map nodes') + '" aria-label="'
     + v23T('搜索思维导图', 'Search mind map') + '">'
     + '<select id="v26MapScope" aria-label="' + v23T('历史覆盖范围', 'History coverage') + '">'
     + '<option value="24">' + v23T('快速 · 24 会话', 'Quick · 24 conversations') + '</option>'
     + '<option value="48" selected>' + v23T('标准 · 48 会话', 'Standard · 48 conversations') + '</option>'
     + '<option value="80">' + v23T('广泛 · 80 会话', 'Broad · 80 conversations') + '</option></select>'
     + '<div class="v26-map-zoom"><button type="button" id="v26ZoomOut" aria-label="'
     + v23T('缩小', 'Zoom out') + '">−</button>'
     + '<span id="v26ZoomValue">100%</span><button type="button" id="v26ZoomIn" aria-label="'
     + v23T('放大', 'Zoom in') + '">＋</button>'
     + '<button type="button" id="v26ZoomReset" aria-label="'
     + v23T('重置缩放', 'Reset zoom') + '">↺</button></div></div>'
     + '<div id="v26MapViewport"><div id="v26MapCanvas"></div></div></section>'
     + '<aside id="v26MapInspector"><div class="v23-empty">'
     + v23T('选择节点查看摘要和来源', 'Select a node to view its summary and sources') + '</div></aside></div>'
     + '<div class="v24-knowledge-workspace" id="v24KnowledgeItems"><aside class="v24-knowledge-sidebar">'
     + '<div class="v24-knowledge-toolbar"><input id="v24KnowledgeSearch" type="search" '
     + 'placeholder="' + v23T('搜索标题或正文', 'Search title or body') + '" aria-label="'
     + v23T('搜索知识', 'Search knowledge') + '">'
     + '<select id="v24KnowledgeStatus" aria-label="' + v23T('按状态筛选', 'Filter by status') + '">'
     + '<option value="">' + v23T('全部状态', 'All statuses') + '</option>'
     + '<option value="draft">' + v23T('草稿', 'Draft') + '</option>'
     + '<option value="verified">' + v23T('已确认', 'Verified') + '</option>'
     + '<option value="conflicted">' + v23T('有冲突', 'Conflicted') + '</option>'
     + '<option value="superseded">' + v23T('已替代', 'Superseded') + '</option></select></div>'
     + '<div id="v24KnowledgeList"></div></aside>'
     + '<main id="v24KnowledgeDetail"><div class="v23-empty">'
     + v23T('选择一条知识查看正文和来源', 'Select a knowledge item to view its body and sources') + '</div></main></div>';
   document.body.appendChild(v24Knowledge);

  const v23OrganizeModal = document.createElement('div');
  v23OrganizeModal.className = 'v23-modal';
  v23OrganizeModal.id = 'v23OrganizeModal';
  v23OrganizeModal.innerHTML = '<div class="v23-modal-panel" role="dialog" aria-modal="true" aria-labelledby="v23OrganizeTitle">'
    + '<h3 id="v23OrganizeTitle">' + v23T('整理会话', 'Organize conversation') + '</h3>'
    + '<label>' + v23T('会话名称', 'Conversation name') + '<input id="v23OrganizeName" maxlength="120"></label>'
    + '<label>' + v23T('项目', 'Project') + '<input id="v23OrganizeProject" maxlength="80" placeholder="'
    + v23T('例如 Copilot Bridge', 'For example, Copilot Bridge') + '"></label>'
    + '<label>' + v23T('标签', 'Labels') + '<input id="v23OrganizeLabels" placeholder="'
    + v23T('用逗号分隔，最多 20 个', 'Comma-separated, up to 20') + '"></label>'
    + '<div class="v23-modal-actions"><button type="button" class="v23-secondary" id="v23DeleteSession">'
    + v23T('删除', 'Delete') + '</button>'
    + '<button type="button" class="v23-secondary" id="v23OrganizeCancel">'
    + v23T('取消', 'Cancel') + '</button>'
    + '<button type="button" class="v23-primary" id="v23OrganizeSave">'
    + v23T('保存', 'Save') + '</button></div></div>';
  document.body.appendChild(v23OrganizeModal);

   const v24KnowledgeModal = document.createElement('div');
   v24KnowledgeModal.className = 'v23-modal';
   v24KnowledgeModal.id = 'v24KnowledgeModal';
   v24KnowledgeModal.innerHTML = '<div class="v23-modal-panel v24-knowledge-modal-panel" role="dialog" '
     + 'aria-modal="true" aria-labelledby="v24KnowledgeModalTitle">'
     + '<h3 id="v24KnowledgeModalTitle">' + v23T('从当前会话保存知识', 'Save knowledge from current conversation') + '</h3>'
     + '<div class="v24-knowledge-form-grid"><label>' + v23T('类型', 'Type') + '<select id="v24KnowledgeType">'
     + '<option value="summary">' + v23T('摘要', 'Summary') + '</option>'
     + '<option value="fact">' + v23T('事实', 'Fact') + '</option>'
     + '<option value="decision">' + v23T('决策', 'Decision') + '</option>'
     + '<option value="procedure">' + v23T('操作步骤', 'Procedure') + '</option>'
     + '<option value="solution">' + v23T('解决方案', 'Solution') + '</option>'
     + '<option value="failure">' + v23T('失败尝试', 'Failed attempt') + '</option>'
     + '<option value="code_pattern">' + v23T('代码方案', 'Code pattern') + '</option>'
     + '<option value="todo">' + v23T('待办', 'To-do') + '</option>'
     + '<option value="question">' + v23T('未决问题', 'Open question') + '</option></select></label>'
     + '<label>' + v23T('标题', 'Title') + '<input id="v24KnowledgeTitle" maxlength="160"></label>'
     + '<label>' + v23T('项目', 'Project') + '<input id="v24KnowledgeProject" maxlength="80"></label>'
     + '<label>' + v23T('标签', 'Labels') + '<input id="v24KnowledgeLabels" placeholder="'
     + v23T('用逗号分隔', 'Comma-separated') + '"></label></div>'
     + '<label>' + v23T('知识正文', 'Knowledge body') + '<textarea id="v24KnowledgeBody" rows="8"></textarea></label>'
     + '<fieldset class="v24-evidence-picker"><legend>' + v23T('来源消息', 'Source messages') + '</legend>'
     + '<div id="v24EvidenceChoices"></div></fieldset>'
     + '<div class="v23-modal-actions"><button type="button" class="v23-secondary" '
     + 'id="v24KnowledgeCancel">' + v23T('取消', 'Cancel') + '</button><button type="button" class="v23-primary" '
     + 'id="v24KnowledgeSave">' + v23T('保存草稿', 'Save draft') + '</button></div></div>';
   document.body.appendChild(v24KnowledgeModal);

  function v23SetActiveNav(view) {
    v23Rail.querySelectorAll('.v23-nav').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
  }

  function v23ShowView(view, loadKnowledge = true) {
    v23State.navigationRevision += 1;
    v23State.view = view;
    document.body.dataset.productView = view;
    drawer.classList.toggle('show', view === 'sessions');
    inboxDrawer.classList.toggle('show', view === 'inbox');
    v23Settings.classList.toggle('show', view === 'settings');
    v24Knowledge.classList.toggle('show', view === 'knowledge');
    for (const [element, elementView] of [
      [conversationView, 'conversation'], [drawer, 'sessions'],
      [inboxDrawer, 'inbox'], [v23Settings, 'settings'],
      [v24Knowledge, 'knowledge'],
    ]) {
      const active = view === elementView;
      element.inert = !active;
      element.setAttribute('aria-hidden', String(!active));
    }
    timeline.inert = true;
    timeline.setAttribute('aria-hidden', 'true');
    inboxDrawer.classList.remove('detail-open');
    timeline.classList.remove('show');
    scrim.classList.remove('show');
    v23SetActiveNav(view);
    if (view === 'sessions') loadSessions();
    if (view === 'inbox') loadDashboard(false);
    if (view === 'settings') v23RenderSettings();
    if (view === 'knowledge' && loadKnowledge) v26OpenKnowledgeHub();
  }

  function v23Count(filter) {
    if (filter === 'favorite') return sessions.filter(item => item.isFavorite || item.isPinned).length;
    if (filter === 'recent') return sessions.filter(item => (item.updatedAt || 0) >= Date.now() / 1000 - 7 * 86400).length;
    if (filter === 'waiting') return sessions.filter(item => item.awaitingResponse).length;
    if (filter.startsWith('machine:')) return sessions.filter(item => item.machineId === filter.slice(8)).length;
    if (filter.startsWith('project:')) return sessions.filter(item => item.project === filter.slice(8)).length;
    return sessions.length;
  }

  function v23FilterButton(filter, label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'v23-filter-btn' + (v23State.sessionFilter === filter ? ' active' : '');
    const text = document.createElement('span');
    text.textContent = label;
    const count = document.createElement('span');
    count.className = 'v23-filter-count';
    count.textContent = String(v23Count(filter));
    button.append(text, count);
    button.onclick = () => {
      v23State.sessionFilter = filter;
      renderSessions();
    };
    return button;
  }

  function v23FilterSection(title, entries) {
    const section = document.createElement('div');
    section.className = 'v23-filter-section';
    const heading = document.createElement('div');
    heading.className = 'v23-filter-title';
    heading.textContent = title;
    section.appendChild(heading);
    for (const [filter, label] of entries) section.appendChild(v23FilterButton(filter, label));
    return section;
  }

  function v23SessionMatches(item) {
    if (hideShortSessions && item.id !== sid && isShortSession(item)) return false;
    const filter = v23State.sessionFilter;
    if (filter === 'favorite' && !item.isFavorite && !item.isPinned) return false;
    if (filter === 'recent' && (item.updatedAt || 0) < Date.now() / 1000 - 7 * 86400) return false;
    if (filter === 'waiting' && !item.awaitingResponse) return false;
    if (filter.startsWith('machine:') && item.machineId !== filter.slice(8)) return false;
    if (filter.startsWith('project:') && item.project !== filter.slice(8)) return false;
    const query = v23State.sessionQuery.trim().toLowerCase();
    if (!query) return true;
    return [item.title, item.machineName, item.project, ...(item.labels || [])]
      .join(' ').toLowerCase().includes(query);
  }

  async function v23PatchSession(item, changes) {
    try {
      await api('PATCH', '/api/sessions/' + encodeURIComponent(item.id), changes);
      await loadSessions();
      v23RenderSettings();
    } catch (error) {
      alert(v23T('更新会话失败：', 'Could not update conversation: ') + error.message);
    }
  }

  function v23OpenOrganizer(item) {
    v23State.organizeSession = item;
    document.querySelector('#v23OrganizeName').value = item.title || '';
    document.querySelector('#v23OrganizeProject').value = item.project || '';
    document.querySelector('#v23OrganizeLabels').value = (item.labels || []).join(', ');
    v23OrganizeModal.classList.add('show');
    document.querySelector('#v23OrganizeName').focus();
  }

  function v23CloseOrganizer() {
    v23State.organizeSession = null;
    v23OrganizeModal.classList.remove('show');
  }

  renderSessions = function renderV23Sessions() {
    slist.innerHTML = '';
    const manager = document.createElement('div');
    manager.className = 'v23-session-manager';
    const sidebar = document.createElement('aside');
    sidebar.className = 'v23-session-sidebar';
    sidebar.append(
      v23FilterSection(v23T('快捷访问', 'Quick access'), [
        ['all', v23T('全部会话', 'All conversations')],
        ['favorite', v23T('收藏与置顶', 'Favorites and pinned')],
        ['recent', v23T('最近使用', 'Recently used')],
        ['waiting', v23T('等待回复', 'Awaiting response')],
      ]),
    );
    const machines = [...new Map(sessions.filter(item => item.machineId)
      .map(item => [item.machineId, item.machineName || item.machineId.slice(0, 8)])).entries()];
    if (machines.length) {
      sidebar.append(v23FilterSection(v23T('设备', 'Devices'), machines.map(([id, name]) => ['machine:' + id, name])));
    }
    const projects = [...new Set(sessions.map(item => item.project).filter(Boolean))].sort();
    if (projects.length) {
      sidebar.append(v23FilterSection(v23T('项目', 'Projects'), projects.map(project => ['project:' + project, project])));
    }

    const main = document.createElement('main');
    main.className = 'v23-session-main';
    const toolbar = document.createElement('div');
    toolbar.className = 'v23-session-toolbar';
    const search = document.createElement('input');
    search.type = 'search';
    search.placeholder = v23T(
      '筛选会话、设备、项目或标签',
      'Filter conversations, devices, projects, or labels',
    );
    search.value = v23State.sessionQuery;
    search.oninput = () => {
      v23State.sessionQuery = search.value;
      renderSessions();
      requestAnimationFrame(() => {
        const input = slist.querySelector('.v23-session-toolbar input');
        if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
      });
    };
    toolbar.appendChild(search);
    main.append(toolbar, shortFilterControl(sessions, renderSessions));

    const visible = sessions.filter(v23SessionMatches);
    if (!visible.length) {
      const empty = document.createElement('div');
      empty.className = 'v23-empty';
      empty.textContent = sessions.length
        ? v23T('当前筛选条件下没有会话。', 'No conversations match the current filters.')
        : v23T('还没有会话。', 'No conversations yet.');
      main.appendChild(empty);
    } else {
      const table = document.createElement('div');
      table.className = 'v23-session-table';
      for (const item of visible) {
        const row = document.createElement('div');
        row.className = 'v23-session-row' + (item.id === sid ? ' active' : '');

        const favorite = document.createElement('button');
        favorite.type = 'button';
        favorite.className = 'v23-icon-btn' + (item.isFavorite ? ' on' : '');
        favorite.title = item.isFavorite
          ? v23T('取消收藏', 'Remove from favorites') : v23T('收藏', 'Add to favorites');
        favorite.setAttribute('aria-label', favorite.title);
        favorite.textContent = item.isFavorite ? '★' : '☆';
        favorite.onclick = () => v23PatchSession(item, { isFavorite: !item.isFavorite });

        const title = document.createElement('div');
        title.className = 'v23-session-title';
        const titleText = document.createElement('strong');
        titleText.textContent = item.title || v23T('未命名会话', 'Untitled conversation');
        const titleMeta = document.createElement('span');
        titleMeta.textContent = v23T(
          (item.promptCount || 0) + ' 轮问答'
            + (item.awaitingResponse ? ' · 等待回复' : ''),
          (item.promptCount || 0) + ' prompts'
            + (item.awaitingResponse ? ' · Awaiting response' : ''),
        );
        title.append(titleText, titleMeta);
        title.onclick = () => openSession(item.id);

        const source = document.createElement('div');
        source.className = 'v23-session-source';
        source.textContent = item.machineName || 'Unknown device';
        const updated = document.createElement('div');
        updated.className = 'v23-session-time';
        updated.textContent = relTime(item.updatedAt);

        const labels = document.createElement('div');
        labels.className = 'v23-session-labels';
        if (item.project) {
          const project = document.createElement('span');
          project.className = 'v23-project';
          project.textContent = item.project;
          labels.appendChild(project);
        }
        for (const label of (item.labels || []).slice(0, 2)) {
          const tag = document.createElement('span');
          tag.className = 'v23-label';
          tag.textContent = label;
          labels.appendChild(tag);
        }

        const generateKnowledge = document.createElement('button');
        generateKnowledge.type = 'button';
        generateKnowledge.className = 'v23-icon-btn v27-generate-knowledge';
        generateKnowledge.title = v23T(
          '生成知识库和思维导图', 'Generate knowledge and mind map'
        );
        generateKnowledge.setAttribute('aria-label', generateKnowledge.title);
        generateKnowledge.dataset.sessionId = item.id;
        generateKnowledge.innerHTML = v23Icons.knowledgeGenerate;
        v28SetGenerateButton(generateKnowledge, item.id, true);
        generateKnowledge.onclick = event => {
          event.stopPropagation();
          v27GenerateConversationKnowledge(item.id, generateKnowledge);
        };

        const pin = document.createElement('button');
        pin.type = 'button';
        pin.className = 'v23-icon-btn v23-pin' + (item.isPinned ? ' on' : '');
        pin.title = item.isPinned
          ? v23T('取消置顶', 'Unpin') : v23T('置顶', 'Pin');
        pin.setAttribute('aria-label', pin.title);
        pin.textContent = item.isPinned ? '●' : '○';
        pin.onclick = () => v23PatchSession(item, { isPinned: !item.isPinned });

        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'v23-icon-btn';
        more.title = v23T('整理会话', 'Organize conversation');
        more.setAttribute('aria-label', more.title);
        more.textContent = '⋯';
        more.onclick = () => v23OpenOrganizer(item);
        row.append(
          favorite, title, source, updated, labels, generateKnowledge, pin, more
        );
        table.appendChild(row);
      }
      main.appendChild(table);
    }
    manager.append(sidebar, main);
    slist.appendChild(manager);
  };

  function v23InboxItemVisible(item) {
    if (v23State.inboxFilter === 'all') return true;
    if (v23State.inboxFilter === 'running') return false;
    if (item.remindAt && item.remindAt > Date.now() / 1000) return false;
    return item.status !== 'completed' && item.status !== 'ignored';
  }

  function v23RenderInboxFilters() {
    v23InboxFilters.innerHTML = '';
    const entries = [
      ['attention', v23T('待处理', 'Attention')],
      ['all', v23T('全部', 'All')],
      ['running', v23T('正在运行', 'Running')],
    ];
    for (const [filter, label] of entries) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'v23-filter-chip' + (v23State.inboxFilter === filter ? ' active' : '');
      button.textContent = label;
      button.onclick = () => {
        v23State.inboxFilter = filter;
        v23State.selectedInboxId = '';
        renderDashboard();
      };
      v23InboxFilters.appendChild(button);
    }
  }

  async function v23RenderInboxDetail(item) {
    const request = ++v23State.detailRequest;
    if (!item) {
      v23InboxDetail.innerHTML = '<div class="v23-empty">'
        + v23T('当前筛选条件下没有消息。', 'No messages match the current filter.') + '</div>';
      return;
    }
    v23InboxDetail.innerHTML = '';
    const head = document.createElement('div');
    head.className = 'v23-detail-head';
    const identity = document.createElement('div');
    const source = document.createElement('span');
    source.className = 'sourcepill';
    source.textContent = sourceLabel(item.source);
    const heading = document.createElement('h3');
    heading.textContent = item.title || v23T('未命名会话', 'Untitled conversation');
    const meta = document.createElement('div');
    meta.className = 'inboxmeta';
    meta.textContent = relTime(item.createdAt)
      + (item.remindAt ? v23T(' · 提醒于 ', ' · Reminder at ')
        + new Date(item.remindAt * 1000).toLocaleString() : '');
    identity.append(source, heading, meta);

    const actions = document.createElement('div');
    actions.className = 'v23-detail-actions';
    const back = document.createElement('button');
    back.type = 'button'; back.className = 'v23-secondary'; back.textContent = v23T('返回', 'Back');
    back.onclick = () => inboxDrawer.classList.remove('detail-open');
    const remind = document.createElement('button');
    remind.type = 'button'; remind.className = 'v23-secondary'; remind.textContent = v23T('1 小时后提醒', 'Remind in 1 hour');
    remind.onclick = async () => {
      await api('POST', '/api/inbox/' + encodeURIComponent(item.id) + '/remind', { minutes: 60 });
      v23State.selectedInboxId = '';
      inboxDrawer.classList.remove('detail-open');
      await loadDashboard(false);
    };
    const complete = document.createElement('button');
    complete.type = 'button'; complete.className = 'v23-primary'; complete.textContent = v23T('处理完成', 'Complete');
    complete.onclick = async () => {
      await api('PATCH', '/api/inbox/' + encodeURIComponent(item.id), { status: 'completed' });
      v23State.selectedInboxId = '';
      inboxDrawer.classList.remove('detail-open');
      await loadDashboard(false);
    };
    const open = document.createElement('button');
    open.type = 'button'; open.className = 'v23-primary';
    open.textContent = item.conversationId
      ? v23T('打开会话', 'Open conversation') : v23T('导入并打开', 'Import and open');
    open.onclick = async () => {
      if (item.conversationId) await openSession(item.conversationId);
      else if (item.sourceKey) await importCopilot(item.sourceKey);
    };
    actions.append(back, remind, complete, open);
    head.append(identity, actions);

    const summary = document.createElement('div');
    summary.className = 'v23-detail-summary';
    summary.textContent = item.summary || v23T('(无摘要)', '(no summary)');
    const thread = document.createElement('div');
    thread.className = 'v23-detail-thread';
    v23InboxDetail.append(head, summary, thread);

    if (!item.conversationId) return;
    try {
      const session = await api('GET', '/api/sessions/' + encodeURIComponent(item.conversationId));
      if (request !== v23State.detailRequest) return;
      const messages = (session.messages || []).slice(-8);
      if (!messages.length) {
        thread.innerHTML = '<div class="v23-empty">'
          + v23T('这个会话还没有消息。', 'This conversation has no messages yet.') + '</div>';
        return;
      }
      for (const message of messages) {
        const bubble = document.createElement('div');
        bubble.className = 'v23-detail-message ' + (message.role === 'user' ? 'user' : 'ai');
        bubble.textContent = message.text || v23T('(无文本)', '(no text)');
        thread.appendChild(bubble);
      }
    } catch (error) {
      if (request === v23State.detailRequest) {
        thread.innerHTML = '<div class="v23-empty">'
          + v23T('读取会话失败：', 'Could not load conversation: ')
          + esc(error.message) + '</div>';
      }
    }
  }

  async function v23SelectInbox(item) {
    v23State.selectedInboxId = item.id;
    if (item.status === 'unread') {
      try {
        await api('PATCH', '/api/inbox/' + encodeURIComponent(item.id), { status: 'seen' });
        item.status = 'seen';
      } catch (_) { }
    }
    inboxDrawer.classList.add('detail-open');
    v23EnhanceInbox();
  }

  function v23EnhanceInbox() {
    const state = dashboardState || { inbox: [], activeJobs: [] };
    v23RenderInboxFilters();
    const rows = [...inboxList.querySelectorAll('.inboxitem')];
    const jobRows = [...inboxList.querySelectorAll('.jobrow')];
    const showJobs = v23State.inboxFilter === 'running' || v23State.inboxFilter === 'all';
    jobRows.forEach(row => { row.hidden = !showJobs; });
    const visibleItems = [];
    (state.inbox || []).forEach((item, index) => {
      const row = rows[index];
      if (!row) return;
      const visible = v23InboxItemVisible(item);
      row.hidden = !visible;
      row.classList.toggle('selected', item.id === v23State.selectedInboxId);
      row.onclick = () => v23SelectInbox(item);
      if (visible) visibleItems.push(item);
    });
    inboxList.querySelectorAll('.inbox-section').forEach(heading => {
      if (heading.textContent === v23T('正在运行', 'Running')) heading.hidden = !showJobs;
      if (heading.textContent === v23T('最近回复', 'Recent responses')) {
        heading.hidden = v23State.inboxFilter === 'running';
      }
    });
    const selected = visibleItems.find(item => item.id === v23State.selectedInboxId)
      || visibleItems[0] || null;
    if (selected) v23State.selectedInboxId = selected.id;
    rows.forEach((row, index) => row.classList.toggle(
      'selected', !!selected && state.inbox[index] && state.inbox[index].id === selected.id,
    ));
    v23RenderInboxDetail(selected);
  }

  const v24TypeLabels = {
    summary: v23T('摘要', 'Summary'), fact: v23T('事实', 'Fact'),
    decision: v23T('决策', 'Decision'), procedure: v23T('操作步骤', 'Procedure'),
    solution: v23T('解决方案', 'Solution'), failure: v23T('失败尝试', 'Failed attempt'),
    code_pattern: v23T('代码方案', 'Code pattern'), todo: v23T('待办', 'To-do'),
    question: v23T('未决问题', 'Open question'),
  };
  const v24StatusLabels = {
    draft: v23T('草稿', 'Draft'), verified: v23T('已确认', 'Verified'),
    conflicted: v23T('有冲突', 'Conflicted'), superseded: v23T('已替代', 'Superseded'),
  };
  const v26MapKindLabels = {
    topic: v23T('主题', 'Topic'), project: v23T('项目', 'Project'),
    goal: v23T('目标', 'Goal'), decision: v23T('决策', 'Decision'),
    practice: v23T('实践', 'Practice'), problem: v23T('问题', 'Problem'),
    solution: v23T('方案', 'Solution'), failure: v23T('失败', 'Failure'),
    question: v23T('未决', 'Open'), todo: v23T('待办', 'To-do'),
  };

  let v28KnowledgePollTimer = null;

  function v28KnowledgeJob(conversationId) {
    return v23State.knowledgeJobs.find(
      job => job.conversationId === String(conversationId || '')
    ) || null;
  }

  function v28KnowledgeJobActive(job) {
    return !!job && (job.status === 'queued' || job.status === 'running');
  }

  function v28KnowledgePhase(job) {
    if (job.status === 'failed') return v23T('生成失败', 'Failed');
    if (job.status === 'completed') return v23T('已完成', 'Completed');
    if (job.phase === 'mapping') return v23T('正在生成思维导图', 'Building mind map');
    if (job.phase === 'extracting') return v23T('正在提炼会话知识', 'Extracting knowledge');
    return v23T('等待开始', 'Waiting to start');
  }

  function v28SetGenerateButton(button, conversationId, compact = false) {
    if (!button) return;
    const active = v23State.knowledgeSubmissions.has(String(conversationId || ''))
      || v28KnowledgeJobActive(v28KnowledgeJob(conversationId));
    button.disabled = active;
    if (active) {
      button.setAttribute('aria-busy', 'true');
      button.title = v23T('知识库生成中', 'Generating knowledge');
      button.setAttribute('aria-label', button.title);
      button.innerHTML = '<span class="v28-spinner" aria-hidden="true"></span>'
        + (compact ? '' : '<span class="v28-generate-label">'
          + v23T('生成中…', 'Generating…') + '</span>');
    } else {
      button.removeAttribute('aria-busy');
      button.title = v23T('生成知识库和思维导图', 'Generate knowledge and mind map');
      button.setAttribute('aria-label', button.title);
      button.innerHTML = v23Icons.knowledgeGenerate;
    }
  }

  function v28RefreshGenerateButtons() {
    v28SetGenerateButton(document.querySelector('#generateKnowledgeBtn'), sid);
    document.querySelectorAll('.v27-generate-knowledge').forEach(button => {
      v28SetGenerateButton(button, button.dataset.sessionId, true);
    });
  }

  function v28RenderKnowledgeJobs() {
    const panel = document.querySelector('#v28KnowledgeJobs');
    const list = document.querySelector('#v28KnowledgeJobList');
    if (!panel || !list) return;
    const jobs = v23State.knowledgeJobs.slice(0, 6);
    panel.hidden = !jobs.length;
    list.innerHTML = '';
    for (const job of jobs) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'v28-job-row status-' + job.status;
      row.disabled = job.status !== 'completed';
      const heading = document.createElement('span');
      heading.className = 'v28-job-heading';
      const title = document.createElement('strong');
      title.textContent = job.conversationTitle || v23T('未命名会话', 'Untitled conversation');
      const phase = document.createElement('span');
      phase.className = 'v28-job-phase';
      phase.textContent = v28KnowledgePhase(job);
      heading.append(title, phase);
      const progress = document.createElement('progress');
      progress.max = 100;
      progress.value = Math.max(0, Math.min(100, Number(job.progressPercent) || 0));
      const meta = document.createElement('span');
      meta.className = 'v28-job-meta';
      if (job.status === 'failed') {
        meta.textContent = job.error || v23T('未知错误', 'Unknown error');
      } else if (job.phase === 'extracting') {
        meta.textContent = v23T(
          '已处理 ' + (job.processedMessageCount || 0) + ' 条消息 · 剩余 '
            + (job.remainingMessageCount || 0) + ' 条',
          (job.processedMessageCount || 0) + ' messages processed · '
            + (job.remainingMessageCount || 0) + ' remaining',
        );
      } else {
        meta.textContent = v23T(
          '已生成 ' + (job.generatedItemCount || 0) + ' 条知识',
          (job.generatedItemCount || 0) + ' knowledge items generated',
        );
      }
      row.append(heading, progress, meta);
      if (job.status === 'completed') row.onclick = () => v28OpenKnowledgeJob(job);
      list.appendChild(row);
    }
  }

  async function v28OpenKnowledgeJob(job) {
    if (!job || job.status !== 'completed') return;
    try {
      const result = await api(
        'GET', '/api/knowledge/map?conversationId=' + encodeURIComponent(job.conversationId)
      );
      if (!result.map) throw new Error(v23T('生成结果不存在', 'Generated map is missing'));
      v23State.knowledgeMapRequest += 1;
      v23State.knowledgeMap = result.map;
      v23State.selectedMapNodeId = '';
      v23State.knowledgeMapQuery = '';
      v23State.collapsedMapNodes.clear();
      v23State.displayedKnowledgeJobId = job.conversationId + ':' + job.completedAt;
      v23State.knowledgeMode = 'map';
      v23ShowView('knowledge', false);
      v26SetKnowledgeMode('map', false);
      v26RenderKnowledgeMap(true);
    } catch (error) {
      alert(v23T('读取生成结果失败：', 'Could not load generated result: ') + error.message);
    }
  }

  function v28ScheduleKnowledgeJobs(delay) {
    clearTimeout(v28KnowledgePollTimer);
    v28KnowledgePollTimer = window.setTimeout(v28LoadKnowledgeJobs, delay);
  }

  async function v28LoadKnowledgeJobs() {
    if (!dashboardInitialized) {
      v28ScheduleKnowledgeJobs(500);
      return;
    }
    const request = ++v23State.knowledgeJobsRequest;
    try {
      const result = await api('GET', '/api/knowledge/generation-jobs');
      if (request !== v23State.knowledgeJobsRequest) return;
      const pending = v23State.knowledgeJobs.filter(
        job => v23State.knowledgeSubmissions.has(job.conversationId)
      );
      v23State.knowledgeJobs = [...pending, ...(result.jobs || []).filter(
        job => !v23State.knowledgeSubmissions.has(job.conversationId)
      )];
      v28RenderKnowledgeJobs();
      v28RefreshGenerateButtons();
      const current = v28KnowledgeJob(sid);
      const displayedId = current && current.conversationId + ':' + current.completedAt;
      if (current && current.status === 'completed'
        && v23State.view === 'knowledge'
        && displayedId !== v23State.displayedKnowledgeJobId) {
        await v28OpenKnowledgeJob(current);
      }
      v28ScheduleKnowledgeJobs(
        v23State.knowledgeJobs.some(v28KnowledgeJobActive) ? 1000 : 8000
      );
    } catch (_) {
      v28ScheduleKnowledgeJobs(8000);
    }
  }

  function v26SetKnowledgeMode(mode, load = true) {
    v23State.knowledgeMode = mode === 'list' ? 'list' : 'map';
    const mapMode = v23State.knowledgeMode === 'map';
    document.querySelector('#v26KnowledgeMap').hidden = !mapMode;
    document.querySelector('#v24KnowledgeItems').hidden = mapMode;
    const mapTab = document.querySelector('#v26MapTab');
    const listTab = document.querySelector('#v26ListTab');
    mapTab.classList.toggle('active', mapMode);
    listTab.classList.toggle('active', !mapMode);
    mapTab.setAttribute('aria-selected', String(mapMode));
    listTab.setAttribute('aria-selected', String(!mapMode));
    if (load) {
      if (mapMode) v26LoadKnowledgeMap();
      else v24LoadKnowledge();
    }
  }

  function v26OpenKnowledgeHub() {
    v26SetKnowledgeMode(v23State.knowledgeMode);
    v28LoadKnowledgeJobs();
  }

  function v26MapVisibility(nodes, query) {
    if (!query) return new Set(nodes.map(node => node.id));
    const needle = query.toLowerCase();
    const byId = new Map(nodes.map(node => [node.id, node]));
    const children = new Map();
    for (const node of nodes) {
      if (!children.has(node.parentId || '')) children.set(node.parentId || '', []);
      children.get(node.parentId || '').push(node);
    }
    const visible = new Set(nodes.filter(node => [
      node.label, node.summary, v26MapKindLabels[node.kind] || node.kind,
    ].join(' ').toLowerCase().includes(needle)).map(node => node.id));
    for (const id of [...visible]) {
      let parentId = byId.get(id) && byId.get(id).parentId;
      while (parentId) { visible.add(parentId); parentId = byId.get(parentId).parentId; }
      const queue = [...(children.get(id) || [])];
      while (queue.length) {
        const child = queue.shift(); visible.add(child.id);
        queue.push(...(children.get(child.id) || []));
      }
    }
    return visible;
  }

  function v26RevealSelectedMapNode(attempt = 0) {
    if (!window.matchMedia('(max-width: 700px)').matches) return;
    const viewport = document.querySelector('#v26MapViewport');
    const selected = document.querySelector('#v26MapCanvas .v26-map-node.selected');
    if (!viewport || !selected) return;
    window.setTimeout(() => {
      const viewportRect = viewport.getBoundingClientRect();
      const selectedRect = selected.getBoundingClientRect();
      if ((!viewport.clientWidth || !viewport.clientHeight || !selectedRect.width)
        && attempt < 3) {
        v26RevealSelectedMapNode(attempt + 1);
        return;
      }
      viewport.scrollTo({
        left: Math.max(0, viewport.scrollLeft + selectedRect.left - viewportRect.left
          - (viewport.clientWidth - selectedRect.width) / 2),
        top: Math.max(0, viewport.scrollTop + selectedRect.top - viewportRect.top
          - (viewport.clientHeight - selectedRect.height) / 2),
      });
    }, attempt ? 100 : 0);
  }

  function v26MapNode(node, childrenByParent, visible) {
    const children = (childrenByParent.get(node.id) || []).filter(child => visible.has(child.id));
    const branch = document.createElement('div');
    branch.className = 'v26-map-branch';
    const row = document.createElement('div');
    row.className = 'v26-map-node-row';
    if (children.length) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'v26-map-toggle';
      const collapsed = v23State.collapsedMapNodes.has(node.id)
        && !v23State.knowledgeMapQuery;
      toggle.textContent = collapsed ? '+' : '−';
      const searchActive = Boolean(v23State.knowledgeMapQuery);
      toggle.disabled = searchActive;
      toggle.setAttribute('aria-label', searchActive
        ? v23T('清除搜索后可折叠', 'Clear search to collapse branches')
        : (collapsed ? v23T('展开分支', 'Expand branch') : v23T('折叠分支', 'Collapse branch')));
      toggle.onclick = event => {
        event.stopPropagation();
        if (collapsed) v23State.collapsedMapNodes.delete(node.id);
        else v23State.collapsedMapNodes.add(node.id);
        v26RenderKnowledgeMap();
      };
      row.appendChild(toggle);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'v26-map-node kind-' + node.kind
      + (node.id === v23State.selectedMapNodeId ? ' selected' : '');
    const kind = document.createElement('span');
    kind.className = 'v26-map-kind';
    kind.textContent = v26MapKindLabels[node.kind] || node.kind;
    const label = document.createElement('strong'); label.textContent = node.label;
    const summary = document.createElement('span');
    summary.textContent = v24Excerpt(node.summary, 120);
    const evidence = document.createElement('small');
    evidence.textContent = v23T(
      (node.evidence || []).length + ' 条来源'
        + (node.missingEvidenceCount ? ' · ' + node.missingEvidenceCount + ' 条已失效' : ''),
      (node.evidence || []).length + ' sources'
        + (node.missingEvidenceCount ? ' · ' + node.missingEvidenceCount + ' unavailable' : ''),
    );
    button.append(kind, label, summary, evidence);
    button.onclick = () => {
      v23State.selectedMapNodeId = node.id;
      v26RenderKnowledgeMap(true);
    };
    row.appendChild(button);
    branch.appendChild(row);
    if (children.length && (
      !v23State.collapsedMapNodes.has(node.id) || v23State.knowledgeMapQuery
    )) {
      const childList = document.createElement('div');
      childList.className = 'v26-map-children';
      for (const child of children) {
        childList.appendChild(v26MapNode(child, childrenByParent, visible));
      }
      branch.appendChild(childList);
    }
    return branch;
  }

  function v26RenderMapInspector() {
    const inspector = document.querySelector('#v26MapInspector');
    const mindMap = v23State.knowledgeMap;
    const node = mindMap && (mindMap.nodes || []).find(
      item => item.id === v23State.selectedMapNodeId
    );
    inspector.innerHTML = '';
    if (!mindMap) {
      inspector.innerHTML = '<div class="v23-empty">'
        + v23T('整理历史后可在这里查看节点证据', 'Organize history to inspect node evidence here') + '</div>';
      return;
    }
    if (!node) {
      const title = document.createElement('h3'); title.textContent = mindMap.title;
      const summary = document.createElement('p'); summary.textContent = mindMap.summary;
      inspector.append(title, summary);
      return;
    }
    const kind = document.createElement('span');
    kind.className = 'v26-map-inspector-kind kind-' + node.kind;
    kind.textContent = v26MapKindLabels[node.kind] || node.kind;
    const title = document.createElement('h3'); title.textContent = node.label;
    const summary = document.createElement('p'); summary.textContent = node.summary;
    const reuse = v23Action(v23T('在当前会话中引用', 'Use in current conversation'), () => {
      const sources = (node.evidence || []).slice(0, 8).map(item =>
        '- ' + v24Excerpt(item.conversationTitle || v23T('原始会话', 'Source conversation'), 80)
        + (item.turnOrdinal ? ' / Turn ' + item.turnOrdinal : '')
      ).join('\n');
      const header = v23T(
        '请基于以下历史知识节点继续讨论。先核对适用范围和来源。\n\n',
        'Continue based on the following historical knowledge node. First verify its scope and sources.\n\n',
      ) + '# ' + node.label + '\n\n';
      const footer = v23T('\n\n来源：\n', '\n\nSources:\n') + sources
        + v23T('\n\n我的问题：', '\n\nMy question:');
      input.value = header + node.summary.slice(0, Math.max(
        0, 8000 - header.length - footer.length
      )) + footer;
      v23ShowView('conversation');
      input.dispatchEvent(new Event('input'));
      input.focus();
    }, true);
    const evidenceTitle = document.createElement('h4');
    evidenceTitle.textContent = v23T('来源证据', 'Source evidence');
    inspector.append(kind, title, summary, reuse, evidenceTitle);
    if (node.missingEvidenceCount) {
      const stale = document.createElement('p');
      stale.className = 'v26-map-stale';
      stale.textContent = v23T(
        node.missingEvidenceCount + ' 条来源已不存在。重新整理历史后将自动更新此节点。',
        node.missingEvidenceCount + ' sources are unavailable. Organize history again to update this node.',
      );
      inspector.appendChild(stale);
    }
    for (const item of node.evidence || []) {
      const source = document.createElement('button');
      source.type = 'button'; source.className = 'v26-map-evidence';
      const sourceTitle = document.createElement('strong');
      sourceTitle.textContent = item.conversationTitle || v23T('原始会话', 'Source conversation');
      const snippet = document.createElement('span'); snippet.textContent = item.snippet;
      source.append(sourceTitle, snippet);
      source.onclick = () => v24OpenEvidence(
        item.conversationId, item.turnId, item.messageId
      );
      inspector.appendChild(source);
    }
  }

  function v26RenderKnowledgeMap(revealSelection = false) {
    const canvas = document.querySelector('#v26MapCanvas');
    const mindMap = v23State.knowledgeMap;
    canvas.innerHTML = '';
    if (!mindMap || !(mindMap.nodes || []).length) {
      const empty = document.createElement('div');
      empty.className = 'v26-map-empty';
      const title = document.createElement('strong');
      title.textContent = v23T('尚未生成历史思维导图', 'No history mind map yet');
      const detail = document.createElement('span');
      detail.textContent = v23T('点击“整理历史”开始', 'Select “Organize history” to begin');
      empty.append(title, detail); canvas.appendChild(empty);
      document.querySelector('#v26MapCoverage').textContent = v23T('尚未生成', 'Not generated');
      v26RenderMapInspector();
      return;
    }
    const nodes = mindMap.nodes || [];
    if (!nodes.some(node => node.id === v23State.selectedMapNodeId)) {
      v23State.selectedMapNodeId = (nodes.find(node => !node.parentId) || nodes[0]).id;
    }
    const root = document.createElement('div'); root.className = 'v26-map-root';
    const rootTitle = document.createElement('strong'); rootTitle.textContent = mindMap.title;
    const rootSummary = document.createElement('span');
    rootSummary.textContent = v24Excerpt(mindMap.summary, 180);
    root.append(rootTitle, rootSummary);
    const childrenByParent = new Map();
    for (const node of nodes) {
      const key = node.parentId || '';
      if (!childrenByParent.has(key)) childrenByParent.set(key, []);
      childrenByParent.get(key).push(node);
    }
    const visible = v26MapVisibility(nodes, v23State.knowledgeMapQuery.trim());
    const tree = document.createElement('div'); tree.className = 'v26-map-tree';
    for (const node of (childrenByParent.get('') || []).filter(item => visible.has(item.id))) {
      tree.appendChild(v26MapNode(node, childrenByParent, visible));
    }
    canvas.append(root, tree);
    canvas.style.transform = 'scale(' + v23State.knowledgeMapScale + ')';
    document.querySelector('#v26ZoomValue').textContent =
      Math.round(v23State.knowledgeMapScale * 100) + '%';
    document.querySelector('#v26MapCoverage').textContent = v23T(
      '覆盖 ' + (mindMap.sourceConversationCount || 0) + ' 个会话 · '
        + (mindMap.sourceMessageCount || 0) + ' 条来源消息 · '
        + relTime(mindMap.updatedAt),
      (mindMap.sourceConversationCount || 0) + ' conversations · '
        + (mindMap.sourceMessageCount || 0) + ' source messages · '
        + relTime(mindMap.updatedAt),
    );
    v26RenderMapInspector();
    if (revealSelection) v26RevealSelectedMapNode();
  }

  async function v26LoadKnowledgeMap() {
    const request = ++v23State.knowledgeMapRequest;
    const canvas = document.querySelector('#v26MapCanvas');
    canvas.innerHTML = '<div class="v23-empty">'
      + v23T('正在读取思维导图…', 'Loading mind map…') + '</div>';
    try {
      const result = await api('GET', '/api/knowledge/map');
      if (request !== v23State.knowledgeMapRequest) return;
      v23State.knowledgeMap = result.map || null;
      v26RenderKnowledgeMap(true);
    } catch (error) {
      if (request !== v23State.knowledgeMapRequest) return;
      canvas.innerHTML = '<div class="v23-empty">'
        + v23T('读取思维导图失败：', 'Could not load mind map: ')
        + esc(error.message) + '</div>';
    }
  }

  async function v26GenerateKnowledgeMap() {
    const button = document.querySelector('#v26GenerateMap');
    const original = button.textContent;
    button.disabled = true; button.textContent = v23T('分析中…', 'Analyzing…');
    document.querySelector('#v26MapCoverage').textContent = v23T(
      '正在整理会话历史…', 'Organizing conversation history…'
    );
    try {
      const result = await api('POST', '/api/knowledge/map/generate', {
        force: !!v23State.knowledgeMap,
        maxConversations: Number(document.querySelector('#v26MapScope').value),
      });
      v23State.knowledgeMapRequest += 1;
      v23State.knowledgeMap = result.map;
      v23State.knowledgeMapEligibleCount = result.eligibleConversationCount || 0;
      v23State.selectedMapNodeId = '';
      v23State.collapsedMapNodes.clear();
      v26SetKnowledgeMode('map', false);
      v26RenderKnowledgeMap(true);
    } catch (error) {
      alert(v23T('历史思维导图生成失败：', 'Could not generate history mind map: ')
        + error.message);
      v26RenderKnowledgeMap();
    } finally {
      button.disabled = false; button.textContent = original;
    }
  }

  function v26SetMapScale(value) {
    v23State.knowledgeMapScale = Math.max(.65, Math.min(1.35, value));
    const canvas = document.querySelector('#v26MapCanvas');
    canvas.style.transform = 'scale(' + v23State.knowledgeMapScale + ')';
    document.querySelector('#v26ZoomValue').textContent =
      Math.round(v23State.knowledgeMapScale * 100) + '%';
  }

  function v24Excerpt(value, limit = 120) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text;
  }

  function v24RenderKnowledgeList() {
    const list = document.querySelector('#v24KnowledgeList');
    list.innerHTML = '';
    if (!v23State.knowledgeItems.length) {
      list.innerHTML = '<div class="v23-empty">'
        + v23T('还没有符合条件的知识。', 'No knowledge items match yet.') + '</div>';
      return;
    }
    for (const item of v23State.knowledgeItems) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'v24-knowledge-row'
        + (item.id === v23State.selectedKnowledgeId ? ' selected' : '');
      const top = document.createElement('span');
      top.className = 'v24-knowledge-row-top';
      const type = document.createElement('span');
      type.className = 'v24-type-badge';
      type.textContent = v24TypeLabels[item.type] || item.type;
      const status = document.createElement('span');
      status.className = 'v24-status-badge ' + item.status;
      status.textContent = v24StatusLabels[item.status] || item.status;
      top.append(type, status);
      const title = document.createElement('strong');
      title.textContent = item.title;
      const excerpt = document.createElement('span');
      excerpt.className = 'v24-knowledge-excerpt';
      excerpt.textContent = v24Excerpt(item.bodyMarkdown);
      const meta = document.createElement('span');
      meta.className = 'v24-knowledge-meta';
      meta.textContent = [item.project, 'v' + (item.versionNumber || 1), v23T(
        (item.evidenceCount || 0) + ' 条证据',
        (item.evidenceCount || 0) + ' evidence sources',
      )].filter(Boolean).join(' · ');
      row.append(top, title, excerpt, meta);
      row.onclick = async () => {
        v23State.selectedKnowledgeId = item.id;
        v24RenderKnowledgeList();
        v24Knowledge.classList.add('detail-open');
        await v24RenderKnowledgeDetail(item.id);
      };
      list.appendChild(row);
    }
  }

  async function v24RenderKnowledgeDetail(itemId) {
    const detail = document.querySelector('#v24KnowledgeDetail');
    const request = ++v23State.knowledgeDetailRequest;
    if (!itemId) {
      detail.innerHTML = '<div class="v23-empty">'
        + v23T('选择一条知识查看正文和来源', 'Select a knowledge item to view its body and sources') + '</div>';
      return;
    }
    detail.innerHTML = '<div class="v23-empty">'
      + v23T('正在读取知识…', 'Loading knowledge…') + '</div>';
    try {
      const item = await api('GET', '/api/knowledge/' + encodeURIComponent(itemId));
      if (request !== v23State.knowledgeDetailRequest) return;
      detail.innerHTML = '';
      const head = document.createElement('div');
      head.className = 'v24-knowledge-detail-head';
      const copy = document.createElement('div');
      const badges = document.createElement('div');
      badges.className = 'v24-detail-badges';
      const type = document.createElement('span');
      type.className = 'v24-type-badge';
      type.textContent = v24TypeLabels[item.type] || item.type;
      const status = document.createElement('span');
      status.className = 'v24-status-badge ' + item.status;
      status.textContent = v24StatusLabels[item.status] || item.status;
      badges.append(type, status);
      const title = document.createElement('h3');
      title.textContent = item.title;
      const meta = document.createElement('div');
      meta.className = 'v24-knowledge-meta';
      meta.textContent = [item.project, 'v' + item.versionNumber,
        relTime(item.updatedAt)].filter(Boolean).join(' · ');
      copy.append(badges, title, meta);
      const actions = document.createElement('div');
      actions.className = 'v24-knowledge-actions';
      const back = v23Action(v23T('返回', 'Back'), () => v24Knowledge.classList.remove('detail-open'), false);
      back.classList.add('v24-mobile-back');
      const verify = v23Action(
        item.status === 'verified' ? v23T('改为草稿', 'Move to draft') : v23T('确认知识', 'Verify knowledge'),
        async () => {
          await api('PATCH', '/api/knowledge/' + encodeURIComponent(item.id), {
            status: item.status === 'verified' ? 'draft' : 'verified',
          });
          await v24LoadKnowledge(item.id);
        }, item.status !== 'verified',
      );
      const reuse = v23Action(v23T('在当前会话中引用', 'Use in current conversation'), () => {
        const sources = (item.evidence || []).slice(0, 8).map(evidence =>
          '- ' + v24Excerpt(evidence.conversationTitle || v23T('原始会话', 'Source conversation'), 80)
          + (evidence.turnOrdinal ? ' / Turn ' + evidence.turnOrdinal : '')
        ).join('\n');
        const header = v23T(
          '请基于以下个人知识继续讨论。先核对其适用范围和来源，再回答我的问题。\n\n',
          'Continue based on the following personal knowledge. Verify its scope and sources before answering.\n\n',
        ) + '# ' + v24Excerpt(item.title, 160) + '\n\n';
        const footer = v23T('\n\n来源：\n', '\n\nSources:\n') + sources
          + v23T('\n\n我的问题：', '\n\nMy question:');
        const bodyLimit = Math.max(0, 8000 - header.length - footer.length);
        input.value = header + (item.bodyMarkdown || '').slice(0, bodyLimit) + footer;
        v23ShowView('conversation');
        input.dispatchEvent(new Event('input'));
        input.focus();
      }, true);
      const remove = v23Action(v23T('删除', 'Delete'), async () => {
        if (!confirm(v23T(
          '删除知识「' + item.title + '」？',
          'Delete knowledge item “' + item.title + '”?',
        ))) return;
        await api('DELETE', '/api/knowledge/' + encodeURIComponent(item.id));
        v23State.selectedKnowledgeId = '';
        v24Knowledge.classList.remove('detail-open');
        await v24LoadKnowledge();
      }, false);
      actions.append(back, reuse, verify, remove);
      head.append(copy, actions);

      const body = document.createElement('article');
      body.className = 'v24-knowledge-body bubble';
      body.innerHTML = mdToHtml(item.bodyMarkdown || '');

      const evidenceSection = document.createElement('section');
      evidenceSection.className = 'v24-detail-section';
      const evidenceHeading = document.createElement('h4');
      evidenceHeading.textContent = v23T('来源证据', 'Source evidence');
      evidenceSection.appendChild(evidenceHeading);
      for (const evidence of item.evidence || []) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'v24-evidence-row';
        const source = document.createElement('strong');
        source.textContent = evidence.conversationTitle || v23T('原始会话', 'Source conversation');
        const snippet = document.createElement('span');
        snippet.textContent = evidence.snippet || v23T('(无文本)', '(no text)');
        const evidenceMeta = document.createElement('small');
        evidenceMeta.textContent = [evidence.role === 'user' ? v23T('用户', 'User') : 'AI',
          evidence.turnOrdinal ? 'Turn ' + evidence.turnOrdinal : ''].filter(Boolean).join(' · ');
        button.append(source, snippet, evidenceMeta);
        button.onclick = () => v24OpenEvidence(
          evidence.conversationId, evidence.turnId, evidence.messageId,
        );
        evidenceSection.appendChild(button);
      }

      const versions = document.createElement('section');
      versions.className = 'v24-detail-section';
      const versionsHeading = document.createElement('h4');
      versionsHeading.textContent = v23T('版本历史', 'Version history');
      versions.appendChild(versionsHeading);
      for (const version of item.versions || []) {
        const row = document.createElement('div');
        row.className = 'v24-version-row';
        row.textContent = 'v' + version.versionNumber + ' · ' + v23T(
          (version.evidenceCount || 0) + ' 条证据',
          (version.evidenceCount || 0) + ' evidence sources',
        ) + ' · ' + relTime(version.createdAt);
        versions.appendChild(row);
      }
      detail.append(head, body, evidenceSection, versions);
    } catch (error) {
      if (request === v23State.knowledgeDetailRequest) {
        detail.innerHTML = '<div class="v23-empty">'
          + v23T('读取知识失败：', 'Could not load knowledge: ')
          + esc(error.message) + '</div>';
      }
    }
  }

  async function v24LoadKnowledge(preferredId = '') {
    const request = ++v23State.knowledgeListRequest;
    try {
      const params = new URLSearchParams();
      if (v23State.knowledgeQuery.trim()) params.set('query', v23State.knowledgeQuery.trim());
      if (v23State.knowledgeStatus) params.set('status', v23State.knowledgeStatus);
      const result = await api('GET', '/api/knowledge' + (params.size ? '?' + params : ''));
      if (request !== v23State.knowledgeListRequest) return;
      v23State.knowledgeItems = result.items || [];
      const requested = preferredId || v23State.selectedKnowledgeId;
      v23State.selectedKnowledgeId = v23State.knowledgeItems.some(item => item.id === requested)
        ? requested : (v23State.knowledgeItems[0] && v23State.knowledgeItems[0].id) || '';
      v24RenderKnowledgeList();
      await v24RenderKnowledgeDetail(v23State.selectedKnowledgeId);
    } catch (error) {
      if (request === v23State.knowledgeListRequest) {
        document.querySelector('#v24KnowledgeList').innerHTML =
          '<div class="v23-empty">' + v23T('读取知识库失败：', 'Could not load Knowledge Hub: ')
          + esc(error.message) + '</div>';
      }
    }
  }

  async function v24OpenEvidence(conversationId, turnId, messageId) {
    if ((input.value.trim() || pending.length) && !confirm(v23T(
      '打开来源会话会清空当前未发送的文字和附件。继续吗？',
      'Opening the source conversation clears unsent text and attachments. Continue?',
    ))) return;
    input.value = '';
    input.style.height = 'auto';
    pending = [];
    renderPending();
    input.dispatchEvent(new Event('input'));
    updateSendEnabled();
    v23PreserveInitialView = false;
    await openSession(conversationId, turnId, messageId);
  }

  function v24CloseKnowledgeModal() {
    v23State.knowledgeSource = null;
    v24KnowledgeModal.classList.remove('show');
  }

  async function v24OpenKnowledgeModal() {
    if (!sid) { alert(v23T('请先打开一个会话。', 'Open a conversation first.')); return; }
    try {
      const session = await api('GET', '/api/sessions/' + encodeURIComponent(sid));
      const messages = session.messages || [];
      if (!messages.length) { alert(v23T('当前会话还没有可作为证据的消息。', 'The current conversation has no messages that can be used as evidence.')); return; }
      const currentTurn = new URLSearchParams(location.search).get('turn')
        || (messages[messages.length - 1] && messages[messages.length - 1].turnId) || '';
      const selected = messages.filter(message => message.turnId === currentTurn);
      const candidates = [...new Map([...selected, ...messages.slice(-12)]
        .map(message => [message.id, message])).values()];
      v23State.knowledgeSource = session;
      document.querySelector('#v24KnowledgeType').value = 'summary';
      document.querySelector('#v24KnowledgeTitle').value = (session.title
        || v23T('未命名会话', 'Untitled conversation')) + v23T(' 摘要', ' summary');
      document.querySelector('#v24KnowledgeProject').value = session.project || '';
      document.querySelector('#v24KnowledgeLabels').value = (session.labels || []).join(', ');
      document.querySelector('#v24KnowledgeBody').value = selected.map(message =>
        '**' + (message.role === 'user' ? v23T('用户', 'User') : 'AI')
        + '**\n\n' + (message.text || '')
      ).join('\n\n');
      const choices = document.querySelector('#v24EvidenceChoices');
      choices.innerHTML = '';
      const selectedIds = new Set(selected.map(message => message.id));
      for (const message of candidates) {
        const label = document.createElement('label');
        label.className = 'v24-evidence-choice';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox'; checkbox.value = message.id;
        checkbox.checked = selectedIds.has(message.id);
        const role = document.createElement('strong');
        role.textContent = message.role === 'user' ? v23T('用户', 'User') : 'AI';
        const text = document.createElement('span');
        text.textContent = v24Excerpt(message.text, 180) || v23T('(无文本)', '(no text)');
        label.append(checkbox, role, text);
        choices.appendChild(label);
      }
      v24KnowledgeModal.classList.add('show');
      document.querySelector('#v24KnowledgeTitle').focus();
    } catch (error) {
      alert(v23T('读取当前会话失败：', 'Could not load current conversation: ')
        + error.message);
    }
  }

  async function v24SaveKnowledge() {
    const button = document.querySelector('#v24KnowledgeSave');
    const evidenceMessageIds = [...document.querySelectorAll(
      '#v24EvidenceChoices input:checked'
    )].map(input => input.value);
    const bodyMarkdown = document.querySelector('#v24KnowledgeBody').value.trim();
    const title = document.querySelector('#v24KnowledgeTitle').value.trim();
    if (!title || !bodyMarkdown || !evidenceMessageIds.length) {
      alert(v23T(
        '标题、正文和至少一条来源消息都是必填项。',
        'Title, body, and at least one source message are required.',
      ));
      return;
    }
    button.disabled = true;
    try {
      const created = await api('POST', '/api/knowledge', {
        type: document.querySelector('#v24KnowledgeType').value,
        title, bodyMarkdown, evidenceMessageIds,
        project: document.querySelector('#v24KnowledgeProject').value,
        labels: document.querySelector('#v24KnowledgeLabels').value
          .split(',').map(value => value.trim()).filter(Boolean),
      });
      v24CloseKnowledgeModal();
      v23State.knowledgeMode = 'list';
      v23ShowView('knowledge');
      await v24LoadKnowledge(created.id);
    } catch (error) {
      alert(v23T('保存知识失败：', 'Could not save knowledge: ') + error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function v25ExtractKnowledge() {
    if (!sid) { alert(v23T('请先打开一个会话。', 'Open a conversation first.')); return; }
    const button = document.querySelector('#v25ExtractKnowledge');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = v23T('提炼中…', 'Extracting…');
    try {
      const result = await api(
        'POST', '/api/sessions/' + encodeURIComponent(sid) + '/knowledge/extract'
      );
      v23State.knowledgeMode = 'list';
      v23ShowView('knowledge');
      const preferred = result.items && result.items[0] && result.items[0].id || '';
      await v24LoadKnowledge(preferred);
      if (result.noNewMessages) alert(v23T('当前会话没有尚未提炼的新消息。', 'The current conversation has no new messages to extract.'));
      else if (!result.items || !result.items.length) alert(v23T('本次没有发现可沉淀的知识。', 'No reusable knowledge was found in this batch.'));
      else if (result.remainingMessageCount) {
        alert(v23T(
          '本批已提炼，仍有 ' + result.remainingMessageCount + ' 条消息待处理。',
          'This batch is complete; ' + result.remainingMessageCount + ' messages remain.',
        ));
      }
    } catch (error) {
      alert(v23T('AI 提炼失败：', 'AI extraction failed: ') + error.message);
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function v27GenerateConversationKnowledge(conversationId, triggerButton) {
    const targetId = String(conversationId || '').trim();
    if (!targetId) { alert(v23T('请先打开一个会话。', 'Open a conversation first.')); return; }
    if (v23State.knowledgeSubmissions.has(targetId)
      || v28KnowledgeJobActive(v28KnowledgeJob(targetId))) return;
    v23State.knowledgeSubmissions.add(targetId);
    v23State.knowledgeJobsRequest += 1;
    const optimisticJob = {
      conversationId: targetId,
      conversationTitle: (sessions.find(item => item.id === targetId) || {}).title || '',
      status: 'queued', phase: 'queued', progressPercent: 0,
      processedMessageCount: 0, remainingMessageCount: 0, generatedItemCount: 0,
      startedAt: Date.now() / 1000, updatedAt: Date.now() / 1000,
    };
    v23State.knowledgeJobs = [optimisticJob, ...v23State.knowledgeJobs.filter(
      job => job.conversationId !== targetId
    )];
    try {
      v28RenderKnowledgeJobs();
      v28RefreshGenerateButtons();
      v26SetKnowledgeMode('map', false);
      v23ShowView('knowledge', false);
      const result = await api(
        'POST', '/api/sessions/' + encodeURIComponent(targetId) + '/knowledge/generate',
        { maxConversations: Number(document.querySelector('#v26MapScope').value) },
      );
      if (!result || !result.job || result.job.conversationId !== targetId) {
        throw new Error(v23T('服务器未确认生成任务。', 'The server did not confirm the generation task.'));
      }
      v23State.knowledgeJobs = [result.job, ...v23State.knowledgeJobs.filter(
        job => job.conversationId !== targetId
      )];
      v28RenderKnowledgeJobs();
      v28RefreshGenerateButtons();
    } catch (error) {
      v23State.knowledgeJobs = v23State.knowledgeJobs.filter(
        job => job !== optimisticJob
      );
      v28RenderKnowledgeJobs();
      alert(v23T('生成知识库失败：', 'Could not start knowledge generation: ') + error.message);
    } finally {
      v23State.knowledgeJobsRequest += 1;
      v23State.knowledgeSubmissions.delete(targetId);
      v28RefreshGenerateButtons();
      v28ScheduleKnowledgeJobs(250);
    }
  }

  function v23SettingSection(title) {
    const section = document.createElement('section');
    section.className = 'v23-settings-section';
    const heading = document.createElement('h3');
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  function v23SettingRow(title, detail, control) {
    const row = document.createElement('div');
    row.className = 'v23-setting-row';
    const copy = document.createElement('div');
    const strong = document.createElement('strong'); strong.textContent = title;
    const text = document.createElement('span'); text.textContent = detail;
    copy.append(strong, text); row.appendChild(copy);
    if (control) row.appendChild(control);
    return row;
  }

  function v23Action(label, handler, primary) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = primary ? 'v23-primary' : 'v23-secondary';
    button.textContent = label;
    button.onclick = handler;
    return button;
  }

  function v23RenderSettings() {
    const grid = document.querySelector('#v23SettingsGrid');
    if (!grid) return;
    grid.innerHTML = '';
    const sync = dashboardState && dashboardState.sync || { enabled: false, auth: {} };
    const auth = sync.auth || {};
    const syncSection = v23SettingSection(v23T('OneDrive 同步', 'OneDrive sync'));
    let syncDetail = v23T('未启用', 'Disabled');
    if (sync.enabled && auth.connected) syncDetail = (auth.username || auth.scope
      || v23T('已连接', 'Connected')) + (sync.pendingEventCount
      ? v23T(' · 待上传 ', ' · Pending upload ') + sync.pendingEventCount
      : v23T(' · 已同步', ' · Synced'));
    else if (sync.enabled && auth.configured) syncDetail = auth.mode === 'local-folder'
      ? v23T('可使用本机 OneDrive 文件夹', 'Local OneDrive folder available')
      : v23T('尚未连接', 'Not connected');
    const syncAction = auth.connected
      ? v23Action(v23T('立即同步', 'Sync now'), () => runSyncAction(
        '/api/sync/run', v23T('正在同步…', 'Syncing…')
      ), true)
      : v23Action(v23T('连接', 'Connect'), () => {
        document.querySelector('#connectSyncBtn').click();
      }, true);
    syncAction.disabled = !sync.enabled || !auth.configured || !!sync.running;
    syncSection.append(
      v23SettingRow(v23T('会话与消息', 'Conversations and messages'), syncDetail, syncAction),
      v23SettingRow(v23T('同步错误', 'Sync error'), sync.lastError || v23T('无', 'None')),
    );

    const deviceSection = v23SettingSection(v23T('已连接设备', 'Connected devices'));
    const devices = [...new Map(sessions.filter(item => item.machineId)
      .map(item => [item.machineId, item.machineName || item.machineId.slice(0, 8)])).entries()];
    if (!devices.length) deviceSection.append(v23SettingRow(
      v23T('当前设备', 'Current device'), v23T('尚无会话来源', 'No conversation sources yet')
    ));
    for (const [id, name] of devices) {
      const count = sessions.filter(item => item.machineId === id).length;
      deviceSection.append(v23SettingRow(name, v23T(
        count + ' 个会话', count + ' conversations'
      )));
    }

    const preferenceSection = v23SettingSection(v23T('偏好', 'Preferences'));
    const languageSelect = document.createElement('select');
    languageSelect.className = 'v23-language-select';
    languageSelect.setAttribute('aria-label', v23T('界面语言', 'Interface language'));
    for (const [value, label] of [['zh', '中文'], ['en', 'English']]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      languageSelect.appendChild(option);
    }
    languageSelect.value = appSettings.uiLanguage === 'en' ? 'en' : 'zh';
    languageSelect.onchange = async () => {
      languageSelect.disabled = true;
      try {
        const requestedLanguage = languageSelect.value;
        appSettings = await api('PATCH', '/api/settings', {
          uiLanguage: requestedLanguage,
        });
        localStorage.setItem('cb_ui_language', requestedLanguage);
        location.reload();
      } catch (error) {
        languageSelect.disabled = false;
        alert(v23T('保存界面语言失败：', 'Could not save interface language: ')
          + error.message);
      }
    };
    preferenceSection.append(
      v23SettingRow(v23T('界面语言', 'Interface language'),
        v23T('仅翻译功能界面，不翻译会话内容', 'Translates product UI, not conversation content'),
        languageSelect),
      v23SettingRow(v23T('输入时间线预览', 'Prompt timeline preview'), v23T(
        (appSettings.promptPreviewLength || 200) + ' 个字符',
        (appSettings.promptPreviewLength || 200) + ' characters',
      ), v23Action(v23T('修改', 'Change'), openModal, false)),
      v23SettingRow(v23T('桌面通知', 'Desktop notifications'),
        !('Notification' in window) ? v23T('浏览器不支持', 'Not supported by browser')
          : Notification.permission === 'granted'
            ? v23T('已允许', 'Allowed') : v23T('未允许', 'Not allowed'),
        v23Action(v23T('设置', 'Set up'), requestBrowserNotifications, false)),
    );

    const securitySection = v23SettingSection(v23T('访问安全', 'Access security'));
    const mode = authCfg && authCfg.authMode || 'tunnel';
    securitySection.append(
      v23SettingRow(v23T('本机访问', 'Local access'), mode === 'tunnel'
        ? v23T('localhost 可信', 'Trusted localhost')
        : v23T('按服务认证策略', 'Uses service authentication policy')),
      v23SettingRow(v23T('远程访问', 'Remote access'), mode === 'tunnel'
        ? v23T('Dev Tunnel Microsoft 登录', 'Dev Tunnel Microsoft sign-in') : mode),
      v23SettingRow(v23T('机密信息', 'Secrets'),
        v23T('不进入会话同步数据', 'Excluded from conversation sync data')),
    );
    grid.append(syncSection, deviceSection, preferenceSection, securitySection);

    const connected = !!auth.connected;
    v23SyncIndicator.textContent = sync.running
      ? v23T('同步中', 'Syncing') : connected ? v23T('已同步', 'Synced') : v23T('仅本地', 'Local only');
  }

  const v23LegacyRenderDashboard = renderDashboard;
  renderDashboard = function renderV23Dashboard() {
    v23LegacyRenderDashboard();
    const unread = dashboardState && dashboardState.unreadCount || 0;
    v23NavBadge.hidden = unread === 0;
    v23NavBadge.textContent = unread > 99 ? '99+' : String(unread);
    v23EnhanceInbox();
    v23RenderSettings();
  };

  const v23LegacyOpenSession = openSession;
  openSession = async function openV23Session(...args) {
    const preserveInitialView = v23PreserveInitialView;
    v23PreserveInitialView = false;
    const navigationRevision = v23State.navigationRevision;
    const result = await v23LegacyOpenSession(...args);
    v28RefreshGenerateButtons();
    if (!preserveInitialView && navigationRevision === v23State.navigationRevision) {
      v23ShowView('conversation');
    }
    return result;
  };
  const v23LegacyNewSession = newSession;
  newSession = async function newV23Session(...args) {
    const preserveInitialView = v23PreserveInitialView;
    v23PreserveInitialView = false;
    const navigationRevision = v23State.navigationRevision;
    const result = await v23LegacyNewSession(...args);
    if (!preserveInitialView && navigationRevision === v23State.navigationRevision) {
      v23ShowView('conversation');
    }
    return result;
  };
  openInboxItem = v23SelectInbox;
  openDrawer = () => v23ShowView('sessions');
  openInbox = async () => { v23ShowView('inbox'); await loadDashboard(false); };

  document.querySelector('#menuBtn').onclick = () => v23ShowView('sessions');
  document.querySelector('#inboxBtn').onclick = () => v23ShowView('inbox');
  document.querySelector('#setBtn').onclick = () => v23ShowView('settings');
  document.querySelector('#newBtn').onclick = newSession;
  document.querySelector('#inboxClose').onclick = () => v23ShowView('conversation');
  document.querySelector('#v23ConnectionSettings').onclick = openModal;
  document.querySelector('#v24NewKnowledge').onclick = v24OpenKnowledgeModal;
  document.querySelector('#v25ExtractKnowledge').onclick = v25ExtractKnowledge;
  document.querySelector('#generateKnowledgeBtn').onclick = event =>
    v27GenerateConversationKnowledge(sid, event.currentTarget);
  document.querySelector('#v26GenerateMap').onclick = v26GenerateKnowledgeMap;
  document.querySelector('#v26MapTab').onclick = () => v26SetKnowledgeMode('map');
  document.querySelector('#v26ListTab').onclick = () => v26SetKnowledgeMode('list');
  document.querySelector('#v26MapSearch').oninput = event => {
    v23State.knowledgeMapQuery = event.target.value;
    v26RenderKnowledgeMap();
  };
  document.querySelector('#v26ZoomOut').onclick = () => v26SetMapScale(
    v23State.knowledgeMapScale - .1
  );
  document.querySelector('#v26ZoomIn').onclick = () => v26SetMapScale(
    v23State.knowledgeMapScale + .1
  );
  document.querySelector('#v26ZoomReset').onclick = () => v26SetMapScale(1);
  let v26ResizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(v26ResizeTimer);
    v26ResizeTimer = setTimeout(() => {
      if (v23State.view === 'knowledge' && v23State.knowledgeMode === 'map') {
        v26RevealSelectedMapNode();
      }
    }, 120);
  });
  document.querySelector('#v24KnowledgeCancel').onclick = v24CloseKnowledgeModal;
  document.querySelector('#v24KnowledgeSave').onclick = v24SaveKnowledge;
  v24KnowledgeModal.addEventListener('click', event => {
    if (event.target === v24KnowledgeModal) v24CloseKnowledgeModal();
  });
  let v24SearchTimer = null;
  document.querySelector('#v24KnowledgeSearch').oninput = event => {
    v23State.knowledgeQuery = event.target.value;
    clearTimeout(v24SearchTimer);
    v24SearchTimer = setTimeout(() => v24LoadKnowledge(), 180);
  };
  document.querySelector('#v24KnowledgeStatus').onchange = event => {
    v23State.knowledgeStatus = event.target.value;
    v23State.selectedKnowledgeId = '';
    v24Knowledge.classList.remove('detail-open');
    v24LoadKnowledge();
  };
  document.querySelector('#markInboxSeenBtn').textContent = v23T('全部完成', 'Complete all');
  document.querySelector('#markInboxSeenBtn').onclick = async () => {
    await api('POST', '/api/inbox/complete-all');
    v23State.selectedInboxId = '';
    await loadDashboard(false);
  };

  const v23SearchInput = v23Search.querySelector('input');
  v23SearchInput.addEventListener('input', () => { v23State.sessionQuery = v23SearchInput.value; });
  v23SearchInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      v23State.sessionQuery = v23SearchInput.value;
      v23ShowView('sessions');
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      v23SearchInput.focus();
    }
  });

  document.querySelector('#v23OrganizeCancel').onclick = v23CloseOrganizer;
  v23OrganizeModal.addEventListener('click', event => {
    if (event.target === v23OrganizeModal) v23CloseOrganizer();
  });
  document.querySelector('#v23OrganizeSave').onclick = async () => {
    const item = v23State.organizeSession;
    if (!item) return;
    const labels = document.querySelector('#v23OrganizeLabels').value
      .split(',').map(value => value.trim()).filter(Boolean);
    await v23PatchSession(item, {
      title: document.querySelector('#v23OrganizeName').value,
      project: document.querySelector('#v23OrganizeProject').value,
      labels,
    });
    v23CloseOrganizer();
  };
  document.querySelector('#v23DeleteSession').onclick = async () => {
    const item = v23State.organizeSession;
    if (!item) return;
    v23CloseOrganizer();
    await delSession(
      item.id, item.title || v23T('未命名会话', 'Untitled conversation')
    );
  };

  const v24InitialView = initialParams.get('dashboard') === '1' ? 'inbox'
    : initialParams.get('knowledge') === '1' ? 'knowledge' : 'conversation';
  v23ShowView(v24InitialView);
  v28ScheduleKnowledgeJobs(0);
})();
