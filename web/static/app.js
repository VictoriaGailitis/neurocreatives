// Глобальное состояние
let state = {
    creatives: [],
    offset: 0,
    limit: 50,
    selectedChannel: null,
    hasMore: true,
    selectedCreativeId: null,
    currentTab: 'gallery',  // 'gallery', 'table' или 'logs'
    tableOffset: 0,
    tableHasMore: true,
    tableData: [],  // Данные для таблицы
    filteredData: [],  // Отфильтрованные данные
    sortColumn: null,
    sortDirection: 'asc',
    filters: {
        channels: [],       // массив выбранных каналов (мультивыбор)
        erMin: null,
        erMax: null,
        viewsMin: null,
        viewsMax: null,
        days: null,         // показывать только посты за последние N дней
        relevantOnly: false, // только потенциальные «целевые креативы»
        dedupe: false       // схлопывать дубли по image_hash, оставляя самый популярный
    },
    logs: {
        eventSource: null,
        paused: false,
        autoScroll: true,
        currentFilter: ''
    },
    scheduleLogs: {
        loaded: 0,  // Количество уже загруженных записей
        total: 0,   // Общее количество записей
        limit: 10   // Количество записей за один раз
    }
};

// ========== ПЛАНИРОВЩИК ==========

// Загрузка настроек планировщика
async function loadScheduleSettings() {
    try {
        const response = await fetch('/api/schedule');
        const data = await response.json();
        
        // Заполняем форму
        document.getElementById('scheduleEnabled').checked = data.enabled || false;
        document.getElementById('scheduleFrequency').value = data.frequency || 'daily';
        document.getElementById('scheduleTime').value = data.time || '08:00';
        
        // Дни недели для custom
        if (data.days && Array.isArray(data.days)) {
            document.querySelectorAll('.schedule-day').forEach(checkbox => {
                checkbox.checked = data.days.includes(parseInt(checkbox.value));
            });
        }
        
        // Срок действия
        const endType = data.end_type || 'indefinite';
        document.querySelector(`input[name="scheduleEndType"][value="${endType}"]`).checked = true;
        if (data.end_date) {
            document.getElementById('scheduleEndDate').value = data.end_date;
        }
        
        // Глубина парсинга
        const parseDepth = data.parse_depth || 'today';
        const parseFromDate = data.parse_from_date || '';
        document.getElementById('parseDepth').value = parseDepth;
        if (parseFromDate) {
            document.getElementById('parseFromDate').value = parseFromDate;
        }
        document.getElementById('parseFromDateGroup').style.display =
            parseDepth === 'from_date' ? 'block' : 'none';
        
        // Обновляем UI
        updateScheduleUI();
        updateScheduleStatus(data);
        
    } catch (error) {
        console.error('Ошибка при загрузке настроек планировщика:', error);
    }
}

// Обновление статуса планировщика
function updateScheduleStatus(data) {
    const statusBadge = document.getElementById('schedulerStatus');
    const nextRunInfo = document.getElementById('nextRunInfo');
    const nextRunTime = document.getElementById('nextRunTime');
    
    if (data.scheduler_running && data.enabled) {
        statusBadge.textContent = 'Запущен';
        statusBadge.classList.add('active');
        
        if (data.next_run) {
            const nextRun = new Date(data.next_run);
            nextRunTime.textContent = nextRun.toLocaleString('ru-RU');
            nextRunInfo.style.display = 'block';
        } else {
            nextRunInfo.style.display = 'none';
        }
    } else {
        statusBadge.textContent = 'Не запущен';
        statusBadge.classList.remove('active');
        nextRunInfo.style.display = 'none';
    }
}

// Обновление UI планировщика
function updateScheduleUI() {
    const enabled = document.getElementById('scheduleEnabled').checked;
    const frequency = document.getElementById('scheduleFrequency').value;
    const endType = document.querySelector('input[name="scheduleEndType"]:checked').value;
    
    // Включение/выключение настроек
    const scheduleSettings = document.getElementById('scheduleSettings');
    if (enabled) {
        scheduleSettings.classList.add('enabled');
    } else {
        scheduleSettings.classList.remove('enabled');
    }
    
    // Показ/скрытие custom дней
    const customDaysGroup = document.getElementById('customDaysGroup');
    if (frequency === 'custom') {
        customDaysGroup.style.display = 'block';
    } else {
        customDaysGroup.style.display = 'none';
    }
    
    // Показ/скрытие даты окончания
    const endDateGroup = document.getElementById('endDateGroup');
    if (endType === 'until_date') {
        endDateGroup.style.display = 'block';
    } else {
        endDateGroup.style.display = 'none';
    }
}

// Сохранение настроек планировщика
async function saveScheduleSettings() {
    const enabled = document.getElementById('scheduleEnabled').checked;
    const frequency = document.getElementById('scheduleFrequency').value;
    const time = document.getElementById('scheduleTime').value;
    const endType = document.querySelector('input[name="scheduleEndType"]:checked').value;
    const endDate = document.getElementById('scheduleEndDate').value;
    
    // Собираем выбранные дни недели
    const days = [];
    document.querySelectorAll('.schedule-day:checked').forEach(checkbox => {
        days.push(parseInt(checkbox.value));
    });
    
    const data = {
        enabled,
        frequency,
        time,
        days,
        end_type: endType,
        end_date: endDate || null,
        parse_depth: document.getElementById('parseDepth').value,
        parse_from_date: document.getElementById('parseFromDate').value || ''
    };
    
    try {
        const response = await fetch('/api/schedule', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });
        
        const result = await response.json();
        
        if (response.ok) {
            alert('Настройки планировщика сохранены');
            // Перезагружаем настройки для обновления статуса
            setTimeout(() => loadScheduleSettings(), 500);
        } else {
            alert('Ошибка: ' + (result.detail || 'Не удалось сохранить'));
        }
    } catch (error) {
        console.error('Ошибка при сохранении настроек планировщика:', error);
        alert('Ошибка сохранения настроек');
    }
}

// Загрузка логов планировщика
async function loadScheduleLogs(loadMore = false) {
    try {
        const limit = loadMore ? state.scheduleLogs.limit : 10;
        const offset = loadMore ? state.scheduleLogs.loaded : 0;
        
        const response = await fetch(`/api/schedule/logs?limit=${limit}&offset=${offset}`);
        const data = await response.json();
        
        const tbody = document.getElementById('scheduleLogsBody');
        const loadMoreBtn = document.getElementById('scheduleLogsLoadMore');
        
        // Обновляем состояние
        if (!loadMore) {
            state.scheduleLogs.loaded = 0;
        }
        
        if (data.logs && data.logs.length > 0) {
            const rows = data.logs.map(log => {
                const timestamp = new Date(log.timestamp).toLocaleString('ru-RU');
                const statusClass = log.status === 'success' ? 'success' : 'error';
                const statusText = log.status === 'success' ? 'Успешно' : 'Ошибка';
                const runType = log.run_type || 'auto';
                const runTypeText = runType === 'manual' ? '🖱️ Ручной' : '⏰ Авто';
                
                return `
                    <tr>
                        <td>${timestamp}</td>
                        <td>${runTypeText}</td>
                        <td><span class="log-status ${statusClass}">${statusText}</span></td>
                        <td>${log.images_parsed || 0}</td>
                        <td>${log.images_analyzed || 0}</td>
                        <td class="log-details">${log.error_message || '—'}</td>
                    </tr>
                `;
            }).join('');
            
            if (loadMore) {
                tbody.innerHTML += rows;
            } else {
                tbody.innerHTML = rows;
            }
            
            state.scheduleLogs.loaded += data.logs.length;
            state.scheduleLogs.total = data.count || data.logs.length;
            
            // Показываем/скрываем кнопку "Показать еще"
            if (state.scheduleLogs.loaded < state.scheduleLogs.total) {
                loadMoreBtn.style.display = 'block';
            } else {
                loadMoreBtn.style.display = 'none';
            }
        } else {
            if (!loadMore) {
                tbody.innerHTML = '<tr><td colspan="6" class="no-data">Нет данных о запусках</td></tr>';
            }
            loadMoreBtn.style.display = 'none';
        }
    } catch (error) {
        console.error('Ошибка при загрузке логов планировщика:', error);
    }
}

// Загрузка дополнительных логов планировщика
function loadMoreScheduleLogs() {
    loadScheduleLogs(true);
}

// Инициализация при загрузке страницы
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadCreatives();
    initEventListeners();
    initTabSwitching();
    initJobStatusPolling();

    // Обновляем статистику каждые 5 секунд
    setInterval(loadStats, 5000);
});

// Инициализация обработчиков событий
function initEventListeners() {
    document.getElementById('btnRunParser').addEventListener('click', runParser);
    document.getElementById('btnRunAnalysis').addEventListener('click', runAnalysis);
    const btnClear = document.getElementById('btnClearDb');
    if (btnClear) btnClear.addEventListener('click', clearCreativesDb);
    document.getElementById('btnRefresh').addEventListener('click', refreshCreatives);
    document.getElementById('btnLoadMore').addEventListener('click', loadMoreCreatives);
    document.getElementById('btnCloseDetail').addEventListener('click', closeDetailPanel);
    
    // Кнопки таблицы
    document.getElementById('btnRefreshTable').addEventListener('click', refreshTable);
    document.getElementById('btnLoadMoreTable').addEventListener('click', loadMoreTable);
    
    // Кнопки фильтров
    document.getElementById('btnApplyFilters').addEventListener('click', applyFilters);
    document.getElementById('btnResetFilters').addEventListener('click', resetFilters);
    
    // Модальное окно настроек
    document.getElementById('btnSettings').addEventListener('click', openSettingsModal);
    document.getElementById('btnCloseSettingsModal').addEventListener('click', closeSettingsModal);
    document.getElementById('btnCancelSettings').addEventListener('click', closeSettingsModal);
    document.getElementById('btnSaveSettings').addEventListener('click', saveSettings);
    
    // Обработчики для планировщика
    document.getElementById('scheduleEnabled').addEventListener('change', updateScheduleUI);
    document.getElementById('scheduleFrequency').addEventListener('change', updateScheduleUI);
    document.querySelectorAll('input[name="scheduleEndType"]').forEach(radio => {
        radio.addEventListener('change', updateScheduleUI);
    });
    document.getElementById('parseDepth').addEventListener('change', function() {
        const parseDepth = this.value;
        const parseFromDateGroup = document.getElementById('parseFromDateGroup');
        parseFromDateGroup.style.display = parseDepth === 'from_date' ? 'block' : 'none';
        if (parseDepth !== 'from_date') {
            document.getElementById('parseFromDate').value = '';
        }
    });
    document.getElementById('btnRefreshScheduleLogs').addEventListener('click', () => loadScheduleLogs(false));
    document.getElementById('btnLoadMoreScheduleLogs').addEventListener('click', loadMoreScheduleLogs);
    
    // Закрытие модального окна по клику на фон
    document.getElementById('settingsModal').addEventListener('click', (e) => {
        if (e.target.id === 'settingsModal') {
            closeSettingsModal();
        }
    });
    
    // Закрытие детальной панели по клику на фон (backdrop)
    document.getElementById('detailPanel').addEventListener('click', (e) => {
        // Закрываем если клик был по самому overlay (класс detail-panel), а не по его дочерним элементам
        if (e.target.classList.contains('detail-panel')) {
            closeDetailPanel();
        }
    });
    
    // Обновление счетчика каналов при вводе
    document.getElementById('channelsTextarea').addEventListener('input', updateChannelsCount);
    
    // Переключение вкладок в настройках
    document.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            switchSettingsTab(e.target.dataset.tab);
        });
    });
    
    // Логи
    document.getElementById('btnPauseLogs').addEventListener('click', toggleLogsPause);
    document.getElementById('btnClearLogs').addEventListener('click', clearLogs);
    document.getElementById('logLevelFilter').addEventListener('change', filterLogs);
}

// Инициализация переключения вкладок
function initTabSwitching() {
    const tabs = document.querySelectorAll('.tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.tab;
            switchTab(tabName);
        });
    });
}

// Переключение вкладок
function switchTab(tabName) {
    state.currentTab = tabName;
    
    // Обновляем активные классы на вкладках
    document.querySelectorAll('.tab').forEach(tab => {
        if (tab.dataset.tab === tabName) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });
    
    // Переключаем видимость контейнеров
    const galleryView = document.getElementById('galleryView');
    const tableView = document.getElementById('tableView');
    const logsView = document.getElementById('logsView');
    const guideView = document.getElementById('guideView');
    
    if (tabName === 'gallery') {
        galleryView.classList.add('active');
        tableView.classList.remove('active');
        logsView.classList.remove('active');
        if (guideView) guideView.classList.remove('active');
    } else if (tabName === 'table') {
        galleryView.classList.remove('active');
        tableView.classList.add('active');
        logsView.classList.remove('active');
        if (guideView) guideView.classList.remove('active');
        // Загружаем данные для таблицы, если еще не загружены
        if (document.getElementById('tableBody').children.length === 1) {
            loadTable();
        }
    } else if (tabName === 'logs') {
        galleryView.classList.remove('active');
        tableView.classList.remove('active');
        logsView.classList.add('active');
        if (guideView) guideView.classList.remove('active');
        // Загружаем логи планировщика
        loadScheduleLogs();
        // Инициализируем SSE для логов, если еще не инициализировано
        if (!state.logs.eventSource) {
            initLogsStream();
        }
    } else if (tabName === 'guide') {
        galleryView.classList.remove('active');
        tableView.classList.remove('active');
        logsView.classList.remove('active');
        if (guideView) guideView.classList.add('active');
    }
}

// Загрузка статистики
async function loadStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();
        
        // Обновляем статистику в сайдбаре
        document.getElementById('statPosts').textContent = data.total_posts;
        document.getElementById('statImages').textContent = data.total_images;
        document.getElementById('statAnalyzed').textContent = data.total_analyzed;
        
        // Обновляем статистику в заголовке вкладок
        document.getElementById('headerStatPosts').textContent = data.total_posts;
        document.getElementById('headerStatImages').textContent = data.total_images;
        document.getElementById('headerStatAnalyzed').textContent = data.total_analyzed;
        
        // Обновляем средние значения под фильтрами
        if (data.avg_er !== undefined) {
            document.getElementById('avgER').textContent = data.avg_er + '%';
        }
        if (data.avg_views !== undefined) {
            document.getElementById('avgViews').textContent = formatNumber(data.avg_views);
        }
        
        // Обновляем список каналов
        renderChannels(data.channels);
        
        // Обновляем выпадающий список каналов для фильтра
        updateChannelsFilter(data.channels);
        
        // Обновляем статусы
        updateStatus('parser', data.parser_status);
        updateStatus('analysis', data.analysis_status);
        
    } catch (error) {
        console.error('Ошибка загрузки статистики:', error);
    }
}

// Отрисовка списка каналов
function renderChannels(channels) {
    const container = document.getElementById('channelsList');
    
    if (!channels || channels.length === 0) {
        container.innerHTML = '<div class="loading">Нет каналов</div>';
        return;
    }
    
    container.innerHTML = channels.map(channel => `
        <div class="channel-item ${state.selectedChannel === channel ? 'active' : ''}"
             onclick="filterByChannel('${channel}')">
            ${channel}
        </div>
    `).join('');
}

// Обновление выпадающего списка каналов для фильтра.
// Это <select multiple>, поэтому одиночной "Все каналы"-опции уже нет —
// «все» = ничего не выбрано. Сохраняем ранее выбранные каналы.
function updateChannelsFilter(channels) {
    const select = document.getElementById('filterChannel');
    if (!select) return;
    const previouslySelected = new Set(
        Array.from(select.selectedOptions || []).map(o => o.value)
    );
    // Если пользователь явно проставлял каналы через state — учитываем и их,
    // чтобы выбор не сбрасывался при обновлении списка по таймеру.
    (state.filters?.channels || []).forEach(c => previouslySelected.add(c));

    select.innerHTML = '';
    if (channels && channels.length > 0) {
        channels.forEach(channel => {
            const option = document.createElement('option');
            option.value = channel;
            option.textContent = channel;
            if (previouslySelected.has(channel)) option.selected = true;
            select.appendChild(option);
        });
    }
}

// Обновление статусов задач
function updateStatus(type, status) {
    const elementId = type === 'parser' ? 'parserStatus' : 'analysisStatus';
    const element = document.getElementById(elementId);
    
    if (status.running) {
        element.textContent = '⏳ ' + status.message;
        element.style.color = 'var(--color-accent)';
    } else {
        element.textContent = status.message;
        element.style.color = 'var(--color-text-secondary)';
    }
}

// Загрузка креативов
async function loadCreatives(append = false) {
    try {
        const params = new URLSearchParams({
            limit: state.limit,
            offset: append ? state.offset : 0
        });

        // Каналы: либо одиночный из сайдбара, либо мультивыбор из верхнего фильтра.
        // Передаём через запятую — бэкенд понимает оба варианта.
        const filterChannels = (state.filters && state.filters.channels) || [];
        let channelsParam = '';
        if (state.selectedChannel) {
            channelsParam = state.selectedChannel;
        } else if (filterChannels.length > 0) {
            channelsParam = filterChannels.join(',');
        }
        if (channelsParam) {
            params.append('channel', channelsParam);
        }

        if (state.filters?.relevantOnly) {
            params.append('relevant_only', 'true');
        }
        if (state.filters?.days) {
            params.append('days', String(state.filters.days));
        }
        if (state.filters?.dedupe) {
            params.append('dedupe', 'true');
        }

        const response = await fetch(`/api/creatives?${params}`);
        const data = await response.json();

        let creatives = data.creatives || [];

        // Клиентские фильтры (ER / просмотры) — серверный API их не знает.
        creatives = applyClientFilters(creatives);

        if (append) {
            state.creatives = [...state.creatives, ...creatives];
        } else {
            state.creatives = creatives;
            state.offset = 0;
        }

        state.offset += (data.creatives || []).length;
        state.hasMore = state.offset < (data.total || 0);

        renderCreatives();

    } catch (error) {
        console.error('Ошибка загрузки креативов:', error);
        document.getElementById('creativesGrid').innerHTML = `
            <div class="loading">Ошибка загрузки креативов</div>
        `;
    }
}

// Клиентская фильтрация по ER / просмотрам / каналу / дате / релевантности.
// Используется и галереей, и таблицей.
function applyClientFilters(items) {
    const f = state.filters || {};
    const channels = Array.isArray(f.channels) ? f.channels : [];
    let cutoffMs = null;
    if (f.days) {
        cutoffMs = Date.now() - Number(f.days) * 86400000;
    }
    return (items || []).filter(c => {
        if (channels.length > 0 && !channels.includes(c.channel)) return false;
        const er = c.er || 0;
        if (f.erMin != null && er < f.erMin) return false;
        if (f.erMax != null && er > f.erMax) return false;
        const views = c.views || 0;
        if (f.viewsMin != null && views < f.viewsMin) return false;
        if (f.viewsMax != null && views > f.viewsMax) return false;
        if (cutoffMs != null && c.date) {
            const d = new Date(c.date).getTime();
            if (!isNaN(d) && d < cutoffMs) return false;
        }
        if (f.relevantOnly) {
            if (!isRelevantCreative(c)) return false;
        }
        return true;
    });
}

// Совпадает по смыслу с серверным `_relevant_creative_filter`.
// Используется при клиентской фильтрации уже загруженных табличных данных.
function isRelevantCreative(c) {
    const a = c && c.analysis;
    if (!a) return false;
    if (a.is_ad_creative !== true) return false;
    if (a.is_text_screenshot === true) return false;
    if (a.is_news_photo === true) return false;
    if (a.ethics_flag === true) return false;
    if (a.moderation_status === 'rejected') return false;
    const appeal = a.solo_image_appeal;
    if (appeal != null && appeal < 4) return false;
    return true;
}

// Отрисовка креативов
function renderCreatives() {
    const container = document.getElementById('creativesGrid');
    
    if (state.creatives.length === 0) {
        container.innerHTML = '<div class="loading">Креативов пока нет. Запустите парсер.</div>';
        document.getElementById('loadMoreContainer').style.display = 'none';
        return;
    }
    
    container.innerHTML = state.creatives.map(creative => {
        const a = creative.analysis || {};
        const emotion = a.emotion || '';
        const er = creative.er || 0;
        const imagePath = creative.image?.file_path || '';
        const tags = Array.isArray(a.tags) ? a.tags : [];
        const citationCount = creative.citation_count || 0;
        const duplicateCount = creative.duplicate_count || 0;
        const moderation = a.moderation_status || '';

        const badges = [];
        if (citationCount > 0) {
            badges.push(`<span class="card-badge badge-citation" title="Найдено цитирований в других каналах">🔁 ${citationCount}</span>`);
        }
        if (duplicateCount > 0) {
            const dupTitle = `Скрыто дублей того же креатива: ${duplicateCount}. Кликните «Показать дубли» в детальной панели.`;
            badges.push(`<span class="card-badge badge-duplicates" title="${dupTitle}">🧬 +${duplicateCount}</span>`);
        }
        if (moderation === 'approved') {
            badges.push(`<span class="card-badge badge-approved" title="Одобрено">✓</span>`);
        } else if (moderation === 'rejected') {
            badges.push(`<span class="card-badge badge-rejected" title="Отклонено">✕</span>`);
        }

        const tagChips = tags.slice(0, 3).map(t => `<span class="tag">${t}</span>`).join('');
        const emotionChip = emotion ? `<span class="tag tag-emotion">${emotion}</span>` : '';

        return `
            <div class="creative-card" onclick="openCreativeDetail(${creative.id})">
                <div class="creative-image-wrap">
                    <img src="/${imagePath}" alt="${creative.channel}" class="creative-image"
                         onerror="this.src='/static/placeholder.png'">
                    ${badges.length ? `<div class="card-badges">${badges.join('')}</div>` : ''}
                </div>
                <div class="creative-content">
                    <div class="creative-channel">${creative.channel}</div>
                    <div class="creative-text">${truncateText(creative.text, 100)}</div>
                    <div class="creative-meta">
                        <div class="meta-item">
                            <span class="meta-label">ER</span>
                            <span class="meta-value">${er}%</span>
                        </div>
                        <div class="meta-item">
                            <span class="meta-label">Просмотров</span>
                            <span class="meta-value">${formatNumber(creative.views)}</span>
                        </div>
                    </div>
                    ${(tagChips || emotionChip) ? `
                        <div class="creative-tags">${tagChips}${emotionChip}</div>
                    ` : ''}
                </div>
            </div>
        `;
    }).join('');
    
    // Показываем кнопку "Загрузить еще"
    const loadMoreContainer = document.getElementById('loadMoreContainer');
    loadMoreContainer.style.display = state.hasMore ? 'block' : 'none';
}

// Открытие детальной панели
async function openCreativeDetail(creativeId) {
    try {
        const response = await fetch(`/api/creative/${creativeId}`);
        const creative = await response.json();
        
        state.selectedCreativeId = creativeId;
        
        const panel = document.getElementById('detailPanel');
        const content = document.getElementById('detailContent');
        
        const image = creative.images[0];
        const analysis = image?.analysis;
        
        content.innerHTML = `
            <img src="/${image.file_path}" alt="${creative.channel}" class="detail-image"
                 onerror="this.src='/static/placeholder.png'">
            
            <div class="detail-section">
                <div class="detail-title">Информация</div>
                <div class="detail-field">
                    <div class="detail-field-label">Канал</div>
                    <div class="detail-field-value">${creative.channel}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Дата публикации</div>
                    <div class="detail-field-value">${formatDate(creative.date)}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Ссылка на пост</div>
                    <div class="detail-field-value">
                        <a href="${creative.post_url}" target="_blank" class="detail-link">
                            Открыть в Telegram
                        </a>
                    </div>
                </div>
            </div>

            ${(() => {
                // Если в текущей ленте этот пост был «лидером» группы дублей,
                // показываем список вытесненных постов с их ER/каналом.
                const cached = (state.creatives || []).find(c => c.id === creativeId);
                const dups = (cached && Array.isArray(cached.duplicates)) ? cached.duplicates : [];
                if (!dups.length) return '';
                const rows = dups.map(d => `
                    <li class="duplicate-row">
                        <a href="${d.post_url || '#'}" target="_blank">${d.channel}</a>
                        <span>· ER ${d.er}%</span>
                        <span>· 👁 ${formatNumber(d.views || 0)}</span>
                        <span class="muted">· ${formatDateShort(d.date)}</span>
                    </li>
                `).join('');
                return `
                <div class="detail-section">
                    <div class="detail-title">🧬 Дубли этого креатива (${dups.length})</div>
                    <p class="muted" style="margin:4px 0 8px 0;font-size:12px;">
                        Этот пост — наиболее популярный из найденных дублей того же изображения.
                        Менее популярные копии скрыты из ленты:
                    </p>
                    <ul class="duplicate-list" style="list-style:none;padding-left:0;margin:0;">
                        ${rows}
                    </ul>
                </div>`;
            })()}

            <div class="detail-section">
                <div class="detail-title">Текст поста</div>
                <div class="detail-field-value">${creative.text}</div>
            </div>
            
            <div class="detail-section">
                <div class="detail-title">Статистика</div>
                <div class="detail-field">
                    <div class="detail-field-label">Просмотров</div>
                    <div class="detail-field-value">${formatNumber(creative.views)}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Пересылок</div>
                    <div class="detail-field-value">${formatNumber(creative.forwards)}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Комментариев</div>
                    <div class="detail-field-value">${formatNumber(creative.replies)}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Реакций</div>
                    <div class="detail-field-value">${formatNumber(creative.reactions)}</div>
                </div>
                <div class="detail-field">
                    <div class="detail-field-label">Engagement Rate</div>
                    <div class="detail-field-value">${creative.er}%</div>
                </div>
            </div>
            
            ${analysis ? `
                ${(() => {
                    // Stage-1 классификация запущена, только если is_ad_creative
                    // или image_is_key_factor НЕ null. Иначе все «Нет» — это
                    // дефолтные значения колонок, которые лучше показывать как «—».
                    const stage1Run = (
                        analysis.is_ad_creative !== null && analysis.is_ad_creative !== undefined
                    ) || (
                        analysis.image_is_key_factor !== null && analysis.image_is_key_factor !== undefined
                    );
                    const dash = '<span class="badge badge-neutral">—</span>';
                    const triBool = (val, yesYes, yesNo) => {
                        if (!stage1Run) return dash;
                        if (val === true) return yesYes;
                        if (val === false) return yesNo;
                        return dash;
                    };
                    return `
                <div class="detail-section">
                    <div class="detail-title">🔍 Этап 1: Классификация ${stage1Run ? '' : '<span class="muted" style="font-size:11px">(не выполнена)</span>'}</div>
                    <div class="detail-field">
                        <div class="detail-field-label">Рекламный креатив</div>
                        <div class="detail-field-value">${triBool(analysis.is_ad_creative,
                            '<span class="badge badge-success">✓ Да</span>',
                            '<span class="badge badge-danger">✕ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Скриншот текста</div>
                        <div class="detail-field-value">${triBool(analysis.is_text_screenshot,
                            '<span class="badge badge-warning">⚠️ Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Новостное фото</div>
                        <div class="detail-field-value">${triBool(analysis.is_news_photo,
                            '<span class="badge badge-warning">⚠️ Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Инфографика</div>
                        <div class="detail-field-value">${triBool(analysis.is_infographic,
                            '<span class="badge badge-info">📊 Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Логотип бренда</div>
                        <div class="detail-field-value">${triBool(analysis.has_brand_logo,
                            '<span class="badge badge-info">🏷️ Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Текст поверх изображения</div>
                        <div class="detail-field-value">${triBool(analysis.has_overlay_text,
                            '<span class="badge badge-info">📝 Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Этические флаги</div>
                        <div class="detail-field-value">${triBool(analysis.ethics_flag,
                            '<span class="badge badge-danger">⛔ Да</span>',
                            '<span class="badge badge-success">✓ Нет</span>')}
                        </div>
                    </div>
                    ${analysis.ethics_reason ? `
                    <div class="detail-field">
                        <div class="detail-field-label">Причина этического флага</div>
                        <div class="detail-field-value">${escapeHtml(analysis.ethics_reason)}</div>
                    </div>` : ''}
                    <div class="detail-field">
                        <div class="detail-field-label">Изображение - ключевой фактор</div>
                        <div class="detail-field-value">
                            ${analysis.image_is_key_factor === true ? '<span class="badge badge-success">✓ Да</span>' :
                              analysis.image_is_key_factor === false ? '<span class="badge badge-danger">✕ Нет</span>' :
                              '<span class="badge badge-neutral">—</span>'}
                        </div>
                    </div>
                    ${analysis.solo_image_appeal != null ? `
                    <div class="detail-field">
                        <div class="detail-field-label">Solo Image Appeal</div>
                        <div class="detail-field-value">
                            <span class="score-badge ${analysis.solo_image_appeal >= 7 ? 'score-high' : analysis.solo_image_appeal >= 4 ? 'score-medium' : 'score-low'}">
                                ${analysis.solo_image_appeal}/10
                            </span>
                        </div>
                    </div>` : ''}
                </div>`;
                })()}

                <div class="detail-section">
                    <div class="detail-title">🎨 Этап 2: Полный анализ</div>
                    <div class="detail-field">
                        <div class="detail-field-label">Тип креатива</div>
                        <div class="detail-field-value">${analysis.creative_type || '—'}</div>
                    </div>
                    ${Array.isArray(analysis.tags) && analysis.tags.length ? `
                    <div class="detail-field">
                        <div class="detail-field-label">Теги</div>
                        <div class="detail-field-value">
                            ${analysis.tags.map(t => `<span class="tag">${t}</span>`).join(' ')}
                        </div>
                    </div>` : ''}
                    <div class="detail-field">
                        <div class="detail-field-label">Сцена</div>
                        <div class="detail-field-value">${analysis.scene || '—'}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Объекты</div>
                        <div class="detail-field-value">${analysis.objects || '—'}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Эмоция</div>
                        <div class="detail-field-value">${analysis.emotion || '—'}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Текст присутствует</div>
                        <div class="detail-field-value">${analysis.text_present ? 'да' : 'нет'}</div>
                    </div>
                    <div class="detail-field">
                        <div class="detail-field-label">Визуальная сила</div>
                        <div class="detail-field-value">${analysis.visual_strength_score || 0}/10</div>
                    </div>
                    ${analysis.solo_appeal_score != null ? `
                    <div class="detail-field">
                        <div class="detail-field-label">Solo appeal</div>
                        <div class="detail-field-value">${analysis.solo_appeal_score}/10</div>
                    </div>` : ''}
                </div>

                <div class="detail-section">
                    <div class="detail-title">🎯 Target Description (для генерации)</div>
                    <div id="targetDescriptionBlock" class="detail-field-value detail-target-description">
                        ${analysis.target_description ? escapeHtml(analysis.target_description) : '<span class="muted">не сгенерировано</span>'}
                    </div>
                    <div class="detail-actions" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
                        <button class="btn btn-secondary" type="button" onclick="generateTargetDescription(${creative.id})">
                            ${analysis.target_description ? '🔁 Перегенерировать' : '✨ Сгенерировать'}
                        </button>
                        ${analysis.target_description ? `<button class="btn btn-secondary" type="button" onclick="copyTargetDescription(${creative.id})">📋 Копировать</button>` : ''}
                    </div>
                </div>

                <div class="detail-section">
                    <div class="detail-title">🛡️ Модерация</div>
                    <div class="detail-field">
                        <div class="detail-field-label">Статус</div>
                        <div class="detail-field-value">
                            <span class="moderation-badge moderation-${analysis.moderation_status || 'pending'}">
                                ${moderationLabel(analysis.moderation_status)}
                            </span>
                        </div>
                    </div>
                    ${analysis.moderation_comment ? `
                    <div class="detail-field">
                        <div class="detail-field-label">Комментарий / причина</div>
                        <div class="detail-field-value">${escapeHtml(analysis.moderation_comment)}</div>
                    </div>` : ''}
                    <div class="detail-actions" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
                        <button class="btn btn-primary" type="button" onclick="moderateCreative(${creative.id}, 'approved')">✓ Одобрить</button>
                        <button class="btn btn-accent" type="button" onclick="moderateCreative(${creative.id}, 'rejected')">✕ Отклонить</button>
                        <button class="btn btn-secondary" type="button" onclick="moderateCreative(${creative.id}, 'pending')">↺ Сбросить</button>
                        <button class="btn btn-secondary" type="button" onclick="reanalyzeCreative(${creative.id})" title="Перезапустить классификацию и анализ для этого изображения">🔄 Переанализировать</button>
                    </div>
                </div>
            ` : '<div class="detail-section"><div class="loading">Анализ еще не выполнен</div></div>'}

            <div class="detail-section">
                <div class="detail-title">🔁 Цитирования в других каналах
                    <span class="muted" style="font-weight:normal">(${creative.citation_count || 0})</span>
                </div>
                <div id="citationsBlock" class="detail-field-value">
                    <div class="loading">Загрузка…</div>
                </div>
            </div>
        `;

        panel.classList.add('open');
        loadCitations(creative.id);

    } catch (error) {
        console.error('Ошибка загрузки деталей:', error);
    }
}

// Закрытие детальной панели
function closeDetailPanel() {
    const panel = document.getElementById('detailPanel');
    panel.classList.remove('open');
    state.selectedCreativeId = null;
}

// Фильтрация по каналу
function filterByChannel(channel) {
    if (state.selectedChannel === channel) {
        state.selectedChannel = null;
    } else {
        state.selectedChannel = channel;
    }
    
    loadCreatives();
    loadStats(); // Обновляем UI каналов
}

// Обновление креативов
function refreshCreatives() {
    state.offset = 0;
    loadCreatives();
}

// Загрузка дополнительных креативов
function loadMoreCreatives() {
    loadCreatives(true);
}

// Запуск парсера
async function runParser() {
    const btn = document.getElementById('btnRunParser');
    btn.disabled = true;

    try {
        const response = await fetch('/api/run-parser', { method: 'POST' });
        const data = await response.json();

        if (data.status === 'already_running') {
            showToast('warn', '📡 Парсер уже запущен', data.message || '');
        } else {
            showToast('info', '📡 Парсер запущен', data.message || '');
        }

        // Сразу обновляем индикаторы — не ждём 5-секундный setInterval
        await refreshJobStatus();
        loadStats();

    } catch (error) {
        console.error('Ошибка запуска парсера:', error);
        showToast('error', 'Ошибка запуска парсера', String(error));
    } finally {
        // Финальное состояние кнопки определяется циклом polling по running-флагу,
        // здесь просто разблокируем — повторный клик заблокируется на сервере.
        btn.disabled = false;
    }
}

// Запуск анализа
async function runAnalysis() {
    const btn = document.getElementById('btnRunAnalysis');
    btn.disabled = true;

    try {
        const response = await fetch('/api/run-analysis', { method: 'POST' });
        const data = await response.json();

        if (data.status === 'already_running') {
            showToast('warn', '🔍 Анализ уже запущен', data.message || '');
        } else {
            showToast('info', '🔍 Анализ запущен', data.message || '');
        }

        await refreshJobStatus();
        loadStats();

    } catch (error) {
        console.error('Ошибка запуска анализа:', error);
        showToast('error', 'Ошибка запуска анализа', String(error));
    } finally {
        btn.disabled = false;
    }
}

// Полная очистка базы креативов (посты, изображения, анализы, цитирования + локальные файлы)
async function clearCreativesDb() {
    const btn = document.getElementById('btnClearDb');
    if (!btn) return;

    const confirm1 = window.confirm(
        'Вы уверены, что хотите ОЧИСТИТЬ базу креативов?\n\n' +
        'Будут удалены ВСЕ:\n' +
        '— посты\n— изображения (включая файлы в downloads/)\n— результаты анализа\n— цитирования\n\n' +
        'Каналы и настройки сохранятся. Действие необратимо.'
    );
    if (!confirm1) return;

    const typed = window.prompt('Для подтверждения введите слово CLEAR (заглавными):', '');
    if ((typed || '').trim().toUpperCase() !== 'CLEAR') {
        showToast('warn', 'Очистка отменена', 'Подтверждение не введено');
        return;
    }

    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Очистка…';

    try {
        const response = await fetch('/api/creatives/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: 'CLEAR', delete_files: true })
        });
        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || 'HTTP ' + response.status);
        }

        const d = data.deleted || {};
        showToast(
            'info',
            '🗑️ База очищена',
            `Удалено: постов ${d.posts || 0}, изображений ${d.images || 0}, ` +
            `анализов ${d.analyses || 0}, цитирований ${d.citations || 0}, ` +
            `файлов ${d.files_deleted || 0}`
        );

        // Перезагружаем UI
        await loadStats();
        await loadCreatives();
        if (typeof loadTable === 'function') loadTable();
    } catch (error) {
        console.error('Ошибка очистки базы:', error);
        showToast('error', 'Ошибка очистки', String(error.message || error));
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}

// =====================================================================
// Job status: статус-бар, карточки в логах, кнопки, тосты
// =====================================================================

// Хранит последний снимок состояния и таймеры
state.jobStatus = {
    parser: null,
    analysis: null,
    pollTimer: null,
    tickerTimer: null,
    // Чтобы тостить «Завершено» только один раз на каждый запуск
    lastFinishedAtToasted: { parser: null, analysis: null },
    // Чтобы при первом приходе данных не плеваться тостами «Завершено» из истории
    primed: false,
};

function initJobStatusPolling() {
    // Клик на ссылку «Открыть логи →»
    const link = document.getElementById('jobStatusOpenLogs');
    if (link) {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            switchTab('logs');
        });
    }

    // Сразу подтягиваем статус, далее регулярные опросы
    refreshJobStatus();
    state.jobStatus.pollTimer = setInterval(refreshJobStatus, 2000);

    // Локальный «тикер» для секундной анимации elapsed-времени без обращения к серверу
    state.jobStatus.tickerTimer = setInterval(updateElapsedCounters, 1000);
}

async function refreshJobStatus() {
    try {
        const res = await fetch('/api/job-status');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        applyJobStatus('parser', data.parser);
        applyJobStatus('analysis', data.analysis);
        state.jobStatus.primed = true;
    } catch (err) {
        // Сетевая ошибка — обновим статус-бар индикатором
        const bar = document.getElementById('jobStatusBar');
        if (bar) bar.dataset.state = 'offline';
        const t1 = document.getElementById('jobParserText');
        const t2 = document.getElementById('jobAnalysisText');
        if (t1) t1.textContent = 'Нет связи с сервером';
        if (t2) t2.textContent = 'Нет связи с сервером';
    }
}

function applyJobStatus(kind, snap) {
    if (!snap) return;
    const prev = state.jobStatus[kind];
    state.jobStatus[kind] = snap;

    // Вычисляем визуальный state
    let stateName = 'idle';
    if (snap.running) stateName = 'running';
    else if (snap.last_status === 'error') stateName = 'error';
    else if (snap.last_status === 'success') stateName = 'success';

    // Status-bar (top)
    const cell = document.querySelector(`.job-status-cell[data-job="${kind}"]`);
    if (cell) cell.dataset.state = stateName;
    const textEl = document.getElementById(kind === 'parser' ? 'jobParserText' : 'jobAnalysisText');
    if (textEl) textEl.textContent = snap.message || '—';
    const iconEl = document.getElementById(kind === 'parser' ? 'jobParserIcon' : 'jobAnalysisIcon');
    if (iconEl) iconEl.textContent = snap.running ? '⏳' : (kind === 'parser' ? '📡' : '🔍');

    // Bar global state (running доминирует)
    const bar = document.getElementById('jobStatusBar');
    if (bar) {
        const anyRunning = state.jobStatus.parser?.running || state.jobStatus.analysis?.running;
        const anyError = state.jobStatus.parser?.last_status === 'error' || state.jobStatus.analysis?.last_status === 'error';
        bar.dataset.state = anyRunning ? 'running' : (anyError ? 'error' : 'idle');
    }

    // Карточка во вкладке Логи
    const cap = kind === 'parser' ? 'Parser' : 'Analysis';
    const card = document.getElementById(`jobStatusCard${cap}`);
    const badge = document.getElementById(`jobStatusCard${cap}Badge`);
    const msg = document.getElementById(`jobStatusCard${cap}Msg`);
    const started = document.getElementById(`jobStatusCard${cap}Started`);
    const duration = document.getElementById(`jobStatusCard${cap}Duration`);
    const typeEl = document.getElementById(`jobStatusCard${cap}Type`);
    const errEl = document.getElementById(`jobStatusCard${cap}Error`);
    if (card) card.dataset.state = stateName;
    if (badge) {
        if (snap.running) { badge.textContent = '⏳ Выполняется'; }
        else if (snap.last_status === 'error') { badge.textContent = '✖ Ошибка'; }
        else if (snap.last_status === 'success') { badge.textContent = '✓ Завершено'; }
        else { badge.textContent = 'Готов'; }
    }
    if (msg) msg.textContent = snap.message || '—';
    if (started) started.textContent = snap.started_at ? formatLocalTime(snap.started_at) : '—';
    if (duration) {
        if (snap.running && snap.started_at) {
            duration.textContent = formatElapsed(elapsedSecondsFromIso(snap.started_at));
        } else if (snap.duration_seconds != null) {
            duration.textContent = formatElapsed(snap.duration_seconds);
        } else {
            duration.textContent = '—';
        }
    }
    if (typeEl) typeEl.textContent = snap.run_type || '—';
    if (errEl) {
        if (snap.last_error && !snap.running) {
            errEl.style.display = 'block';
            errEl.textContent = '⚠ ' + snap.last_error;
        } else {
            errEl.style.display = 'none';
            errEl.textContent = '';
        }
    }

    // Кнопки в шапке: блокируем во время выполнения
    if (kind === 'parser') {
        const btn = document.getElementById('btnRunParser');
        if (btn) btn.disabled = !!snap.running;
    } else {
        const btn = document.getElementById('btnRunAnalysis');
        if (btn) btn.disabled = !!snap.running;
    }

    // Тост о завершении (только при переходе running -> not running, и не из истории)
    if (state.jobStatus.primed && prev && prev.running && !snap.running) {
        const finishedAt = snap.finished_at || '';
        if (state.jobStatus.lastFinishedAtToasted[kind] !== finishedAt) {
            state.jobStatus.lastFinishedAtToasted[kind] = finishedAt;
            const title = (kind === 'parser' ? '📡 Парсер' : '🔍 Анализ');
            if (snap.last_status === 'error') {
                showToast('error', title + ' завершён с ошибкой', snap.last_error || snap.message || '');
            } else {
                showToast('success', title + ' завершён', snap.message || '');
                // Подтягиваем свежие данные после успешного запуска
                loadStats();
                if (typeof refreshCreatives === 'function') refreshCreatives();
            }
        }
    }
}

function updateElapsedCounters() {
    ['parser', 'analysis'].forEach((kind) => {
        const snap = state.jobStatus[kind];
        if (!snap || !snap.running || !snap.started_at) {
            const el = document.getElementById(kind === 'parser' ? 'jobParserElapsed' : 'jobAnalysisElapsed');
            if (el) el.textContent = '';
            return;
        }
        const sec = elapsedSecondsFromIso(snap.started_at);
        const el = document.getElementById(kind === 'parser' ? 'jobParserElapsed' : 'jobAnalysisElapsed');
        if (el) el.textContent = '· ' + formatElapsed(sec);
        const cap = kind === 'parser' ? 'Parser' : 'Analysis';
        const dur = document.getElementById(`jobStatusCard${cap}Duration`);
        if (dur) dur.textContent = formatElapsed(sec);
    });
}

function elapsedSecondsFromIso(iso) {
    try {
        // Сервер шлёт UTC ISO без таймзоны — добавим Z
        const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
        return Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    } catch (_) {
        return 0;
    }
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatElapsed(seconds) {
    if (seconds == null || isNaN(seconds)) return '—';
    const s = Math.round(Number(seconds));
    if (s < 60) return s + ' сек';
    const m = Math.floor(s / 60);
    const rem = s % 60;
    if (m < 60) return m + ' мин ' + rem + ' сек';
    const h = Math.floor(m / 60);
    return h + ' ч ' + (m % 60) + ' мин';
}

function formatLocalTime(iso) {
    try {
        const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
        return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (_) {
        return iso;
    }
}

// Простые тосты вместо alert()
function showToast(kind, title, body) {
    const root = document.getElementById('toastContainer');
    if (!root) {
        // Fallback на случай отсутствия контейнера
        console.log('[toast]', kind, title, body);
        return;
    }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.innerHTML =
        '<div class="toast-title"></div>' +
        '<div class="toast-body"></div>' +
        '<button class="toast-close" aria-label="Закрыть">×</button>';
    el.querySelector('.toast-title').textContent = title || '';
    el.querySelector('.toast-body').textContent = body || '';
    el.querySelector('.toast-close').addEventListener('click', () => el.remove());
    root.appendChild(el);
    // Автоскрытие
    const ttl = kind === 'error' ? 8000 : 4500;
    setTimeout(() => { if (el.parentNode) el.remove(); }, ttl);
}

// Утилиты
function truncateText(text, maxLength) {
    if (!text) return '';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

function formatNumber(num) {
    if (!num) return '0';
    return num.toLocaleString('ru-RU');
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    return date.toLocaleString('ru-RU', {
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// Модальное окно настроек
async function openSettingsModal() {
    try {
        // Источник истины — реестр (таблица channels в БД). Textarea используется
        // только для массового append-добавления и по умолчанию скрыта.
        const channelsTextarea = document.getElementById('channelsTextarea');
        if (channelsTextarea) {
            channelsTextarea.value = '';
            updateChannelsCount();
        }
        const bulk = document.getElementById('bulkAddBlock');
        if (bulk) bulk.style.display = 'none';

        if (typeof window.loadChannelRegistry === 'function') {
            window.loadChannelRegistry();
        }
        
        // Загружаем настройки
        const settingsResponse = await fetch('/api/settings');
        const settingsData = await settingsResponse.json();
        
        // Заполняем поля настроек.
        // OpenAI API ключ берётся ТОЛЬКО из .env (OPENAI_API_KEY) — в UI его нет.
        document.getElementById('promptTextarea').value = settingsData.analysis_prompt || 'Что на этом фото?';
        
        const modal = document.getElementById('settingsModal');
        modal.classList.add('open');
        
        // Загружаем настройки планировщика
        loadScheduleSettings();
        
    } catch (error) {
        console.error('Ошибка загрузки настроек:', error);
        alert('Ошибка загрузки настроек');
    }
}

function closeSettingsModal() {
    const modal = document.getElementById('settingsModal');
    modal.classList.remove('open');
}

// Переключение вкладок настроек
function switchSettingsTab(tabName) {
    // Обновляем активные классы на вкладках
    document.querySelectorAll('.settings-tab').forEach(tab => {
        if (tab.dataset.tab === tabName) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });
    
    // Переключаем видимость панелей
    document.querySelectorAll('.settings-panel').forEach(panel => {
        panel.classList.remove('active');
    });
    
    const panels = {
        'channels': 'settingsChannels',
        'filters': 'settingsFilters',
        'prefilter': 'settingsPrefilter',
        'prompt': 'settingsPrompt',
        'scheduler': 'settingsScheduler'
    };
    
    const activePanel = document.getElementById(panels[tabName]);
    if (activePanel) {
        activePanel.classList.add('active');
    }

    // Подгружаем настройки/метрики предфильтра при открытии вкладки.
    if (tabName === 'prefilter' && typeof window.loadPrefilterSettings === 'function') {
        window.loadPrefilterSettings();
        window.loadPrefilterStats();
    }
}

async function saveSettings() {
    const btn = document.getElementById('btnSaveSettings');
    btn.disabled = true;
    btn.textContent = 'Сохранение...';

    try {
        // Определяем активную вкладку
        const activeTab = document.querySelector('.settings-tab.active').dataset.tab;

        // Если активна вкладка планировщика, сохраняем настройки планировщика
        if (activeTab === 'scheduler') {
            await saveScheduleSettings();
            btn.disabled = false;
            btn.textContent = 'Сохранить';
            return;
        }

        // Вкладка предфильтра — сохраняем её настройки отдельным эндпоинтом.
        if (activeTab === 'prefilter') {
            await window.savePrefilterSettings();
            await window.loadPrefilterStats();
            alert('Настройки предфильтра сохранены');
            btn.disabled = false;
            btn.textContent = 'Сохранить';
            return;
        }

        // Каналы редактируются прямо в реестре (✎ / 🗑 / переключатель «Активен»)
        // и в блоке массового добавления — отдельная кнопка «Добавить в реестр».
        // На общем «Сохранить» каналы уже не трогаем, чтобы случайно не удалить
        // ничего, что пользователь не видел в textarea.

        // Сохраняем настройки.
        // OpenAI API ключ намеренно НЕ передаётся с фронтенда — он хранится только в .env.
        const settings = {
            analysis_prompt: document.getElementById('promptTextarea').value.trim()
        };
        
        const settingsResponse = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });
        
        const settingsData = await settingsResponse.json();
        
        if (settingsResponse.ok) {
            alert('Настройки успешно сохранены');
            closeSettingsModal();
            loadStats(); // Обновляем список каналов в сайдбаре
        } else {
            alert(`Ошибка: ${settingsData.detail}`);
        }
        
    } catch (error) {
        console.error('Ошибка сохранения настроек:', error);
        alert('Ошибка сохранения настроек');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Сохранить';
    }
}

function updateChannelsCount() {
    const textarea = document.getElementById('channelsTextarea');
    if (!textarea) return;
    const channelsText = textarea.value.trim();

    const channels = channelsText
        .split('\n')
        .map(ch => ch.trim().replace(/^@+/, ''))
        .filter(ch => ch.length > 0);

    const countElement = document.getElementById('channelsCount');
    if (countElement) countElement.textContent = channels.length;
}

// ===== Предфильтр (L0/L1/L2) — настройки и метрики воронки =====

window.loadPrefilterSettings = async function () {
    try {
        const resp = await fetch('/api/prefilter-settings');
        if (!resp.ok) return;
        const d = await resp.json();
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };

        chk('pfEnabled', d.enabled);
        set('pfMinTextLen', d.min_text_len ?? 30);
        set('pfStopwords', d.stopwords ?? '');
        set('pfMinViews', d.min_views ?? 0);
        set('pfMinEr', d.min_er ?? 0);
        set('pfLanguages', d.languages ?? '');
        chk('pfDedupeEnabled', d.dedupe_enabled);
        set('pfDedupeThreshold', d.dedupe_threshold ?? 10);
        chk('pfAiFilterEnabled', d.ai_filter_enabled);
        set('pfAiMinSoloAppeal', d.ai_min_solo_appeal ?? 4.0);
        chk('pfAiRejectTextScreenshots', d.ai_reject_text_screenshots);
        chk('pfAiRejectNewsPhotos', d.ai_reject_news_photos);
        chk('pfAiRejectUnethical', d.ai_reject_unethical);
    } catch (e) {
        console.warn('loadPrefilterSettings failed', e);
    }
};

window.savePrefilterSettings = async function () {
    const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
    const checked = (id) => { const el = document.getElementById(id); return el ? el.checked : false; };

    const payload = {
        enabled: checked('pfEnabled'),
        min_text_len: parseInt(val('pfMinTextLen') || '0', 10),
        stopwords: val('pfStopwords') || '',
        min_views: parseInt(val('pfMinViews') || '0', 10),
        min_er: parseFloat(val('pfMinEr') || '0'),
        languages: val('pfLanguages') || '',
        dedupe_enabled: checked('pfDedupeEnabled'),
        dedupe_threshold: parseInt(val('pfDedupeThreshold') || '10', 10),
        ai_filter_enabled: checked('pfAiFilterEnabled'),
        ai_min_solo_appeal: parseFloat(val('pfAiMinSoloAppeal') || '0'),
        ai_reject_text_screenshots: checked('pfAiRejectTextScreenshots'),
        ai_reject_news_photos: checked('pfAiRejectNewsPhotos'),
        ai_reject_unethical: checked('pfAiRejectUnethical'),
    };
    await fetch('/api/prefilter-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
};

window.loadPrefilterStats = async function () {
    const box = document.getElementById('prefilterStats');
    if (!box) return;
    try {
        const resp = await fetch('/api/prefilter-stats');
        if (!resp.ok) { box.textContent = 'Не удалось загрузить метрики'; return; }
        const d = await resp.json();
        const s = d.by_status || {};
        const f = d.funnel_totals || {};
        box.innerHTML = `
            <div style="display:flex;flex-wrap:wrap;gap:14px;">
                <div>Всего постов: <b>${d.total_posts ?? 0}</b></div>
                <div>Прошли: <b>${s.passed ?? 0}</b></div>
                <div>Дубли: <b>${s.duplicate ?? 0}</b></div>
                <div>Отклонены: <b>${s.rejected ?? 0}</b></div>
                <div>Ожидают: <b>${s.pending ?? 0}</b></div>
            </div>
            <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:14px;">
                <div>L0 отсев: <b>${f.rejected_l0 ?? 0}</b></div>
                <div>L1 дубли: <b>${f.rejected_l1 ?? 0}</b></div>
                <div>L2 отсев: <b>${f.rejected_l2 ?? 0}</b></div>
                <div>Полный анализ: <b>${f.passed_full ?? 0}</b></div>
            </div>`;
    } catch (e) {
        box.textContent = 'Ошибка загрузки метрик';
        console.warn('loadPrefilterStats failed', e);
    }
};

window.runPrefilterBackfill = async function () {
    const label = document.getElementById('backfillStatus');
    const btn = document.getElementById('btnBackfillPrefilter');
    if (btn) btn.disabled = true;
    if (label) label.textContent = 'Выполняется…';
    try {
        const resp = await fetch('/api/prefilter-backfill', { method: 'POST' });
        const d = await resp.json();
        if (resp.ok) {
            if (label) label.textContent =
                `Готово: обработано ${d.processed ?? 0}, дубли ${d.duplicates ?? 0}`;
            await window.loadPrefilterStats();
        } else {
            if (label) label.textContent = `Ошибка: ${d.detail || 'не удалось'}`;
        }
    } catch (e) {
        if (label) label.textContent = 'Ошибка backfill';
        console.warn('runPrefilterBackfill failed', e);
    } finally {
        if (btn) btn.disabled = false;
    }
};

// ===== Bulk-add / single-add для реестра каналов =====

window.toggleBulkAdd = function (force) {
    const block = document.getElementById('bulkAddBlock');
    if (!block) return;
    const show = (typeof force === 'boolean')
        ? force
        : (block.style.display === 'none');
    block.style.display = show ? '' : 'none';
    if (show) {
        const ta = document.getElementById('channelsTextarea');
        if (ta) { ta.value = ''; ta.focus(); }
        updateChannelsCount();
    }
};

window.addChannelPrompt = async function () {
    const raw = prompt('Введите username канала (без @):', '');
    if (raw === null) return;
    const username = String(raw).trim().replace(/^@+/, '');
    if (!username) return;
    await _appendChannelsToRegistry([username]);
};

window.bulkAddChannels = async function () {
    const ta = document.getElementById('channelsTextarea');
    if (!ta) return;
    const channels = ta.value
        .split('\n')
        .map(ch => ch.trim().replace(/^@+/, ''))
        .filter(ch => ch.length > 0);
    if (!channels.length) {
        alert('Список пуст. Введите хотя бы один username.');
        return;
    }
    const ok = await _appendChannelsToRegistry(channels);
    if (ok) {
        ta.value = '';
        updateChannelsCount();
        window.toggleBulkAdd(false);
    }
};

/**
 * Append-only добавление каналов в реестр.
 *
 * Чтобы не задеть «деактивацию ручных, отсутствующих в новом списке» в
 * POST /api/channels, мы предварительно объединяем новые имена с уже
 * существующими manual-каналами из реестра и шлём общий список.
 */
async function _appendChannelsToRegistry(newChannels) {
    try {
        const regRes = await fetch('/api/channels');
        const regData = await regRes.json();
        const existingManual = Array.isArray(regData.manual) ? regData.manual : [];
        const seen = new Set(existingManual.map(c => c.toLowerCase()));
        const merged = existingManual.slice();
        let appended = 0;
        for (const ch of newChannels) {
            const key = ch.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            merged.push(ch);
            appended += 1;
        }
        if (!appended) {
            if (typeof showToast === 'function') {
                showToast('info', 'Уже в реестре', 'Все указанные каналы уже добавлены.');
            } else {
                alert('Эти каналы уже есть в реестре.');
            }
            return true;
        }
        const res = await fetch('/api/channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(merged),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        if (typeof showToast === 'function') {
            showToast('info', '✓ Добавлено', `Каналов добавлено: ${appended}`);
        }
        window.loadChannelRegistry();
        return true;
    } catch (err) {
        console.warn('append channels failed', err);
        alert('Не удалось добавить каналы: ' + (err.message || err));
        return false;
    }
}

// ========== ТАБЛИЧНОЕ ПРЕДСТАВЛЕНИЕ ==========

// Загрузка данных для таблицы
async function loadTable(append = false) {
    try {
        const params = new URLSearchParams({
            limit: state.limit,
            offset: append ? state.tableOffset : 0
        });
        
        const response = await fetch(`/api/creatives?${params}`);
        const data = await response.json();
        
        if (append) {
            state.tableData = [...state.tableData, ...data.creatives];
            state.tableOffset += data.creatives.length;
        } else {
            state.tableData = data.creatives;
            state.tableOffset = data.creatives.length;
        }
        
        state.tableHasMore = state.tableOffset < data.total;
        
        // Если активны фильтры, применяем их
        if (state.filters.channel || state.filters.erMin || state.filters.erMax || 
            state.filters.viewsMin || state.filters.viewsMax) {
            applyFilters();
        } else {
            renderTableRows(state.tableData, append);
        }
        
        // Показываем/скрываем кнопку "Загрузить еще"
        const loadMoreContainer = document.getElementById('loadMoreTableContainer');
        loadMoreContainer.style.display = state.tableHasMore ? 'block' : 'none';
        
    } catch (error) {
        console.error('Ошибка загрузки данных для таблицы:', error);
        document.getElementById('tableBody').innerHTML = `
            <tr><td colspan="8" class="loading">Ошибка загрузки данных</td></tr>
        `;
    }
}

// Отрисовка строк таблицы
function renderTableRows(creatives, append = false) {
    const tbody = document.getElementById('tableBody');
    
    if (!append) {
        tbody.innerHTML = '';
    }
    
    if (creatives.length === 0 && !append) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">Креативов пока нет</td></tr>';
        return;
    }
    
    creatives.forEach(creative => {
        const row = document.createElement('tr');
        row.style.cursor = 'pointer';
        
        const imagePath = creative.image?.file_path || '';
        const promptDescription = creative.analysis 
            ? generatePromptDescription(creative)
            : 'Анализ не выполнен';
        
        row.innerHTML = `
            <td class="table-cell-image">
                <img src="/${imagePath}" alt="${creative.channel}" 
                     onerror="this.src='/static/placeholder.png'">
            </td>
            <td class="table-cell-channel">${creative.channel}</td>
            <td class="table-cell-date">${formatDateShort(creative.date)}</td>
            <td class="table-cell-text">
                <div class="table-text-content">${creative.text || ''}</div>
            </td>
            <td class="table-cell-views">${formatNumber(creative.views)}</td>
            <td class="table-cell-er">${creative.er}%</td>
            <td class="table-cell-description">
                <div class="table-text-content">${promptDescription}</div>
            </td>
            <td class="table-cell-actions">
                <button class="btn-table-action" onclick="event.stopPropagation(); downloadImage('/${imagePath}', '${creative.channel}_${creative.id}')">
                    💾 Скачать
                </button>
                <button class="btn-table-action" onclick="event.stopPropagation(); copyPromptDescription(\`${promptDescription.replace(/`/g, '\\`')}\`)">
                    📋 Копировать
                </button>
            </td>
        `;
        
        // Добавляем обработчик клика на строку
        row.addEventListener('click', (e) => {
            // Проверяем, что клик не был по кнопкам действий
            if (!e.target.closest('.btn-table-action')) {
                openCreativeDetail(creative.id);
            }
        });
        
        tbody.appendChild(row);
    });
}

// Генерация текстового описания для промпта
function generatePromptDescription(creative) {
    const analysis = creative.analysis;
    if (!analysis) return 'Нет данных';
    
    const parts = [];
    
    if (analysis.creative_type) {
        parts.push(`Тип: ${analysis.creative_type}`);
    }
    
    if (analysis.scene) {
        parts.push(`Сцена: ${analysis.scene}`);
    }
    
    if (analysis.objects) {
        parts.push(`Объекты: ${analysis.objects}`);
    }
    
    if (analysis.emotion) {
        parts.push(`Эмоция: ${analysis.emotion}`);
    }
    
    if (analysis.text_present) {
        parts.push(`Текст: ${analysis.text_present}`);
    }
    
    return parts.join('. ');
}

// Скачивание изображения
async function downloadImage(imagePath, filename) {
    try {
        const response = await fetch(imagePath);
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        
        const a = document.createElement('a');
        a.href = url;
        a.download = `${filename}.jpg`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
    } catch (error) {
        console.error('Ошибка скачивания изображения:', error);
        alert('Ошибка скачивания изображения');
    }
}

// Копирование описания для промпта
async function copyPromptDescription(text) {
    try {
        await navigator.clipboard.writeText(text);
        
        // Показываем уведомление
        const notification = document.createElement('div');
        notification.className = 'copy-notification';
        notification.textContent = '✓ Скопировано в буфер обмена';
        document.body.appendChild(notification);
        
        setTimeout(() => {
            notification.classList.add('show');
        }, 10);
        
        setTimeout(() => {
            notification.classList.remove('show');
            setTimeout(() => {
                document.body.removeChild(notification);
            }, 300);
        }, 2000);
        
    } catch (error) {
        console.error('Ошибка копирования:', error);
        alert('Ошибка копирования в буфер обмена');
    }
}

// Обновление таблицы
function refreshTable() {
    state.tableOffset = 0;
    loadTable();
}

// Загрузка дополнительных строк
function loadMoreTable() {
    loadTable(true);
}

// Форматирование даты (короткий вариант)
function formatDateShort(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    return date.toLocaleString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// Применение фильтров
function applyFilters() {
    // Читаем значения фильтров
    const channelSelect = document.getElementById('filterChannel');
    const channels = channelSelect
        ? Array.from(channelSelect.selectedOptions).map(o => o.value).filter(Boolean)
        : [];
    const erMinRaw = document.getElementById('filterERMin').value;
    const erMaxRaw = document.getElementById('filterERMax').value;
    const viewsMinRaw = document.getElementById('filterViewsMin').value;
    const viewsMaxRaw = document.getElementById('filterViewsMax').value;
    const daysRaw = document.getElementById('filterDays')?.value || '';
    const relevantOnly = !!document.getElementById('filterRelevantOnly')?.checked;
    const dedupe = !!document.getElementById('filterDedupe')?.checked;
    const erMin = erMinRaw === '' ? null : parseFloat(erMinRaw);
    const erMax = erMaxRaw === '' ? null : parseFloat(erMaxRaw);
    const viewsMin = viewsMinRaw === '' ? null : parseInt(viewsMinRaw);
    const viewsMax = viewsMaxRaw === '' ? null : parseInt(viewsMaxRaw);
    const days = daysRaw === '' ? null : parseInt(daysRaw);

    // Сохраняем в state
    state.filters = {
        channels,
        erMin: Number.isNaN(erMin) ? null : erMin,
        erMax: Number.isNaN(erMax) ? null : erMax,
        viewsMin: Number.isNaN(viewsMin) ? null : viewsMin,
        viewsMax: Number.isNaN(viewsMax) ? null : viewsMax,
        days: Number.isNaN(days) ? null : days,
        relevantOnly,
        dedupe,
    };

    // 1) Галерея на главной — перезапрашиваем с учётом фильтра по каналу
    //    и применяем клиентские фильтры (ER/просмотры).
    state.offset = 0;
    state.selectedChannel = null; // фильтр в баре имеет приоритет над сайдбаром
    if (state.currentTab === 'gallery' || !state.currentTab) {
        loadCreatives(false);
    }

    // 2) Таблица — применяем те же фильтры к уже загруженным данным
    state.filteredData = applyClientFilters(state.tableData);
    if (state.currentTab === 'table') {
        renderTableRows(state.filteredData, false);
        document.getElementById('loadMoreTableContainer').style.display = 'none';
    }
}

// Сброс фильтров
function resetFilters() {
    // Очищаем поля фильтров
    const channelSelect = document.getElementById('filterChannel');
    if (channelSelect) {
        Array.from(channelSelect.options).forEach(o => { o.selected = false; });
    }
    document.getElementById('filterERMin').value = '';
    document.getElementById('filterERMax').value = '';
    document.getElementById('filterViewsMin').value = '';
    document.getElementById('filterViewsMax').value = '';
    const daysEl = document.getElementById('filterDays');
    if (daysEl) daysEl.value = '';
    const relEl = document.getElementById('filterRelevantOnly');
    if (relEl) relEl.checked = false;
    const dedupEl = document.getElementById('filterDedupe');
    if (dedupEl) dedupEl.checked = false;

    // Сбрасываем state
    state.filters = {
        channels: [],
        erMin: null,
        erMax: null,
        viewsMin: null,
        viewsMax: null,
        days: null,
        relevantOnly: false,
        dedupe: false,
    };
    state.filteredData = [];

    // Перезагружаем галерею без фильтров
    state.offset = 0;
    loadCreatives(false);

    // Перерисовываем таблицу со всеми данными
    renderTableRows(state.tableData, false);
    const loadMoreEl = document.getElementById('loadMoreTableContainer');
    if (loadMoreEl) loadMoreEl.style.display = state.tableHasMore ? 'block' : 'none';
}

// ========== ЛОГИ ==========

// Инициализация SSE потока логов
function initLogsStream() {
    // Закрываем предыдущее соединение, если есть
    if (state.logs.eventSource) {
        state.logs.eventSource.close();
    }
    
    // Создаем новое SSE соединение
    const eventSource = new EventSource('/api/logs/stream');
    state.logs.eventSource = eventSource;
    
    eventSource.onopen = () => {
        console.log('SSE соединение установлено');
    };
    
    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            
            // Пропускаем служебные сообщения
            if (data.type === 'connected') {
                console.log(data.message);
                return;
            }
            
            // Добавляем лог, если не на паузе
            if (!state.logs.paused) {
                addLogEntry(data);
            }
        } catch (error) {
            console.error('Ошибка парсинга лога:', error);
        }
    };
    
    eventSource.onerror = (error) => {
        console.error('Ошибка SSE соединения:', error);
        eventSource.close();
        
        // Пытаемся переподключиться через 5 секунд
        setTimeout(() => {
            if (state.currentTab === 'logs') {
                initLogsStream();
            }
        }, 5000);
    };
}

// Добавление записи лога
function addLogEntry(logData) {
    const logsContent = document.getElementById('logsContent');
    const currentFilter = state.logs.currentFilter;
    
    // Проверяем фильтр
    if (currentFilter && logData.level !== currentFilter) {
        return;
    }
    
    // Создаем элемент лога
    const logEntry = document.createElement('div');
    logEntry.className = `log-entry log-${logData.level.toLowerCase()}`;
    
    const timestamp = document.createElement('span');
    timestamp.className = 'log-timestamp';
    timestamp.textContent = `[${logData.timestamp}]`;
    
    const level = document.createElement('span');
    level.className = 'log-level';
    level.textContent = logData.level;
    
    const message = document.createElement('span');
    message.className = 'log-message';
    message.textContent = logData.message;
    
    logEntry.appendChild(timestamp);
    logEntry.appendChild(level);
    logEntry.appendChild(message);
    
    logsContent.appendChild(logEntry);
    
    // Автопрокрутка
    if (state.logs.autoScroll) {
        logsContent.scrollTop = logsContent.scrollHeight;
    }
}

// Переключение паузы логов
function toggleLogsPause() {
    state.logs.paused = !state.logs.paused;
    
    const btn = document.getElementById('btnPauseLogs');
    if (state.logs.paused) {
        btn.innerHTML = '<span class="btn-icon">▶️</span> Продолжить';
        state.logs.autoScroll = false;
    } else {
        btn.innerHTML = '<span class="btn-icon">⏸️</span> Пауза';
        state.logs.autoScroll = true;
    }
}

// Очистка логов
function clearLogs() {
    const logsContent = document.getElementById('logsContent');
    logsContent.innerHTML = '<div class="log-entry log-info">' +
        '<span class="log-timestamp">[--:--:--]</span>' +
        '<span class="log-level">INFO</span>' +
        '<span class="log-message">Логи очищены</span>' +
        '</div>';
}

// Фильтрация логов по уровню
function filterLogs() {
    const filter = document.getElementById('logLevelFilter').value;
    state.logs.currentFilter = filter;
    
    const logsContent = document.getElementById('logsContent');
    const allLogs = logsContent.querySelectorAll('.log-entry');
    
    allLogs.forEach(log => {
        if (!filter) {
            log.style.display = 'flex';
        } else {
            const level = log.querySelector('.log-level').textContent;
            log.style.display = level === filter ? 'flex' : 'none';
        }
    });
}


// ===================================================================
// Phase 2/3/6/7 — Channel filters, discovery, search
// ===================================================================

(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);

    async function fetchJSON(url, options) {
        const resp = await fetch(url, options);
        if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
        return resp.json();
    }

    // ---- Channel filters tab ----
    async function loadChannelFilters() {
        try {
            const data = await fetchJSON('/api/channel-filters');
            if ($('cfMinSubs')) $('cfMinSubs').value = data.min_subscribers ?? 5000;
            if ($('cfMinEr')) $('cfMinEr').value = data.min_er ?? 2.0;
            if ($('cfRequireReactions')) $('cfRequireReactions').checked = !!data.require_reactions;
            if ($('cfMinPpw')) $('cfMinPpw').value = data.min_posts_per_week ?? 1.0;
            if ($('cfKeywords')) $('cfKeywords').value = data.search_keywords || '';
        } catch (e) {
            console.warn('loadChannelFilters failed', e);
        }
    }

    async function saveChannelFilters() {
        const payload = {
            min_subscribers: parseInt($('cfMinSubs').value || '0', 10),
            min_er: parseFloat($('cfMinEr').value || '0'),
            require_reactions: $('cfRequireReactions').checked,
            min_posts_per_week: parseFloat($('cfMinPpw').value || '0'),
            search_keywords: $('cfKeywords').value || '',
        };
        await fetchJSON('/api/channel-filters', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
    }

    // ---- Channel discovery ----
    let discoveryPollTimer = null;

    function showDiscoveryProgress(show) {
        const wrap = $('discoveryProgressWrap');
        if (wrap) wrap.style.display = show ? 'block' : 'none';
    }

    function renderDiscoveryProgress(s) {
        const total = s.total_keywords || 0;
        const done = s.processed_keywords || 0;
        const percent = s.percent || 0;
        const label = $('discoveryProgressLabel');
        const pct = $('discoveryProgressPercent');
        const bar = $('discoveryProgressBar');
        const checked = $('discoveryCheckedCount');
        const accepted = $('discoveryAcceptedCount');
        const events = $('discoveryEvents');
        if (label) label.textContent = `${done} / ${total} ключевых слов` +
            (s.current_keyword ? ` · «${s.current_keyword}»` : '');
        if (pct) pct.textContent = `${percent}%`;
        if (bar) bar.style.width = `${percent}%`;
        if (checked) checked.textContent = s.checked_channels || 0;
        if (accepted) accepted.textContent = s.accepted_channels || 0;
        if (events && Array.isArray(s.events)) {
            const wasAtBottom = Math.abs(events.scrollHeight - events.clientHeight - events.scrollTop) < 20;
            events.innerHTML = s.events.map(line => {
                const safe = line.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                const cls = line.startsWith('✅') ? 'color:#0a0' :
                            line.startsWith('✕') ? 'color:#a00' : '';
                return `<div style="${cls}">${safe}</div>`;
            }).join('');
            if (wasAtBottom) events.scrollTop = events.scrollHeight;
        }
    }

    async function startDiscovery() {
        const status = $('discoveryStatus');
        const results = $('discoveryResults');
        if (status) status.textContent = 'Сохранение настроек…';
        try {
            await saveChannelFilters();
            await fetchJSON('/api/channels/discover', { method: 'POST' });
            if (status) status.textContent = 'Поиск запущен…';
            if (results) results.textContent = '';
            showDiscoveryProgress(true);
            renderDiscoveryProgress({
                total_keywords: 0, processed_keywords: 0, percent: 0,
                checked_channels: 0, accepted_channels: 0, events: [],
            });
            pollDiscovery();
        } catch (e) {
            if (status) status.textContent = `Ошибка: ${e.message}`;
        }
    }

    async function pollDiscovery() {
        clearInterval(discoveryPollTimer);
        discoveryPollTimer = setInterval(async () => {
            try {
                const s = await fetchJSON('/api/channels/discover/status');
                $('discoveryStatus').textContent = s.message || '';
                renderDiscoveryProgress(s);
                if (!s.running) {
                    clearInterval(discoveryPollTimer);
                    const out = (s.results || []).map(r =>
                        `@${r.username} — ${r.subscribers} подп., ER ${r.avg_er}%, ${r.posts_per_week}/нед`
                    );
                    $('discoveryResults').innerHTML = out.length
                        ? '<b>Найдено:</b><br>' + out.join('<br>')
                        : '<i>Каналы не найдены под текущие критерии.</i>';
                }
            } catch (e) {
                clearInterval(discoveryPollTimer);
                $('discoveryStatus').textContent = `Ошибка опроса: ${e.message}`;
            }
        }, 1500);
    }

    // ---- Search bar bound to existing filters bar ----
    function attachSearchBindings() {
        const search = $('filterSearch');
        const tag = $('filterTag');
        const moderation = $('filterModeration');
        const channel = $('filterChannel');
        const days = $('filterDays');
        const relevantOnly = $('filterRelevantOnly');
        const dedupe = $('filterDedupe');
        if (!search) return;
        const apply = () => {
            if (typeof window.applyFilters === 'function') {
                window.applyFilters();
            } else {
                runSearchIntoGrid();
            }
        };
        let debounceTimer;
        search.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(apply, 350);
        });
        if (tag) tag.addEventListener('change', apply);
        if (moderation) moderation.addEventListener('change', apply);
        if (channel) channel.addEventListener('change', apply);
        if (days) days.addEventListener('change', apply);
        if (relevantOnly) relevantOnly.addEventListener('change', apply);
        if (dedupe) dedupe.addEventListener('change', apply);
    }

    async function runSearchIntoGrid() {
        const params = new URLSearchParams();
        const q = $('filterSearch')?.value?.trim();
        const tagEl = $('filterTag');
        const tagList = tagEl
            ? Array.from(tagEl.selectedOptions).map(o => o.value).filter(Boolean)
            : [];
        const moderation = $('filterModeration')?.value;
        const channelEl = $('filterChannel');
        const channels = channelEl
            ? Array.from(channelEl.selectedOptions).map(o => o.value).filter(Boolean)
            : [];
        const erMin = $('filterERMin')?.value;
        const days = $('filterDays')?.value;
        const relevantOnly = $('filterRelevantOnly')?.checked;
        const dedupe = $('filterDedupe')?.checked;
        if (q) params.set('q', q);
        if (tagList.length) params.set('tags', tagList.join(','));
        if (moderation) params.set('moderation', moderation);
        if (channels.length) params.set('channel', channels.join(','));
        if (erMin) params.set('er_min', erMin);
        if (days) params.set('days', days);
        if (relevantOnly) params.set('relevant_only', 'true');
        if (dedupe) params.set('dedupe', 'true');
        params.set('limit', '50');
        try {
            const data = await fetchJSON(`/api/search?${params.toString()}`);
            const grid = $('creativesGrid');
            if (!grid) return;
            if (typeof window.renderCreatives === 'function') {
                window.renderCreatives(data.creatives || []);
                return;
            }
            grid.innerHTML = (data.creatives || []).map(c => {
                const img = c.image && c.image.file_path ? c.image.file_path : '/static/placeholder.png';
                const tags = (c.analysis && c.analysis.tags) ? c.analysis.tags.join(', ') : '';
                return `<div class="creative-card">
                    <img src="${img}" alt="" loading="lazy">
                    <div class="creative-meta">
                        <div><b>${c.channel}</b> · ER ${c.er}% · 👁 ${c.views}</div>
                        <div>${tags}</div>
                    </div>
                </div>`;
            }).join('') || '<div class="loading">Ничего не найдено</div>';
        } catch (e) {
            console.warn('Search failed', e);
        }
    }

    // ---- Wire up after DOM is ready ----
    function init() {
        const btn = $('btnDiscoverChannels');
        if (btn) btn.addEventListener('click', startDiscovery);

        // Load filter settings + reattach to running discovery whenever the tab becomes visible
        const settingsTab = document.querySelector('.settings-tab[data-tab="filters"]');
        if (settingsTab) settingsTab.addEventListener('click', () => {
            loadChannelFilters();
            // If discovery already running on the server — re-attach polling and show progress
            fetchJSON('/api/channels/discover/status').then(s => {
                if (s.running) {
                    showDiscoveryProgress(true);
                    renderDiscoveryProgress(s);
                    pollDiscovery();
                } else if ((s.events && s.events.length) || (s.results && s.results.length)) {
                    // Show last completed run results
                    showDiscoveryProgress(true);
                    renderDiscoveryProgress(s);
                }
            }).catch(() => {});
        });

        // Auto-save on blur
        ['cfMinSubs', 'cfMinEr', 'cfRequireReactions', 'cfMinPpw', 'cfKeywords']
            .forEach(id => {
                const el = $(id);
                if (el) el.addEventListener('change', () => {
                    saveChannelFilters().catch(err => console.warn(err));
                });
            });

        attachSearchBindings();
        // Pre-load once so values are populated when tab is opened
        loadChannelFilters();

        // Кнопка backfill предфильтра.
        const backfillBtn = $('btnBackfillPrefilter');
        if (backfillBtn) backfillBtn.addEventListener('click', window.runPrefilterBackfill);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

/* ---------------------------------------------------------------------------
 * Phase 3-7 frontend extensions:
 *   - Tags / target description / moderation in detail panel
 *   - Cross-channel citations
 *   - Channel registry
 *   - Extended stats
 * Exposed as window.* to be callable from inline onclick handlers.
 * ------------------------------------------------------------------------- */

window.escapeHtml = function (text) {
    if (text == null) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

window.moderationLabel = function (status) {
    switch (status) {
        case 'approved': return '✓ Одобрено';
        case 'rejected': return '✕ Отклонено';
        case 'auto_approved': return '🤖 Авто-одобрено';
        case 'auto_rejected': return '🤖 Авто-отклонено';
        default: return '⏳ Ожидает';
    }
};

window.loadCitations = async function (creativeId) {
    const block = document.getElementById('citationsBlock');
    if (!block) return;
    try {
        const res = await fetch(`/api/creative/${creativeId}/citations`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const list = data.citations || [];
        if (!list.length) {
            block.innerHTML = '<div class="muted">Цитирования не найдены</div>';
            return;
        }
        block.innerHTML = list.map(c => {
            const sim = c.similarity_score != null
                ? `${(c.similarity_score * 100).toFixed(0)}%`
                : '?';
            return `
                <div class="citation-item">
                    <div class="citation-header">
                        <strong>${window.escapeHtml(c.channel || '?')}</strong>
                        <span class="muted">· Похожесть ${sim}</span>
                    </div>
                    ${c.post_url ? `
                        <a href="${c.post_url}" target="_blank" rel="noopener" class="detail-link">
                            Открыть пост в Telegram →
                        </a>
                    ` : ''}
                    <div class="muted" style="font-size:12px">
                        ${c.detected_at ? new Date(c.detected_at).toLocaleString('ru-RU') : ''}
                    </div>
                </div>
            `;
        }).join('');
    } catch (err) {
        console.warn('citations load failed', err);
        block.innerHTML = '<div class="muted">Не удалось загрузить</div>';
    }
};

window.generateTargetDescription = async function (creativeId) {
    const block = document.getElementById('targetDescriptionBlock');
    if (!block) return;
    const original = block.innerHTML;
    block.innerHTML = '<div class="loading">Генерация…</div>';
    try {
        const res = await fetch(`/api/creative/${creativeId}/generate-description`, { method: 'POST' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const text = data.description || data.target_description || data.target_creative_description;
        if (text) {
            block.textContent = text;
        } else {
            block.innerHTML = '<span class="muted">Пустой ответ от модели</span>';
        }
    } catch (err) {
        console.warn('target description generation failed', err);
        block.innerHTML = original;
        alert('Не удалось сгенерировать описание: ' + err.message);
    }
};

window.copyTargetDescription = async function (creativeId) {
    const block = document.getElementById('targetDescriptionBlock');
    if (!block) return;
    const text = block.innerText.trim();
    if (!text) return;
    try {
        await navigator.clipboard.writeText(text);
        const orig = block.innerHTML;
        block.innerHTML = '<span class="muted">✓ Скопировано в буфер обмена</span>';
        setTimeout(() => { block.innerHTML = orig; }, 1200);
    } catch (e) {
        console.warn('clipboard failed', e);
        alert('Не удалось скопировать. Скопируйте вручную.');
    }
};

window.moderateCreative = async function (creativeId, status) {
    try {
        let reason = null;
        if (status === 'rejected') {
            reason = prompt('Причина отклонения (необязательно):') || null;
        }
        const res = await fetch(`/api/creative/${creativeId}/moderate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status, reason })
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        // Reload the detail panel to reflect the new status
        if (typeof window.openCreativeDetail === 'function') {
            await window.openCreativeDetail(creativeId);
        }
        // Refresh card list silently
        if (typeof window.loadCreatives === 'function') {
            window.loadCreatives({ silent: true });
        }
    } catch (err) {
        console.warn('moderation failed', err);
        alert('Не удалось сохранить статус модерации: ' + err.message);
    }
};

window.reanalyzeCreative = async function (creativeId) {
    if (!window.confirm('Перезапустить классификацию и анализ для этого креатива?\nБудут использованы свежие промпты.')) return;
    try {
        if (typeof showToast === 'function') {
            showToast('info', '🔄 Переанализ', 'Запущен повторный анализ…');
        }
        const res = await fetch(`/api/creative/${creativeId}/reanalyze`, { method: 'POST' });
        let data = {};
        try { data = await res.json(); } catch (_) { /* ignore */ }
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        if (typeof showToast === 'function') {
            showToast('info', '✓ Готово', data.rejected_by_filter
                ? `Отклонено фильтром: ${data.rejection_reason || '—'}`
                : 'Классификация и анализ обновлены');
        }
        if (typeof window.openCreativeDetail === 'function') {
            await window.openCreativeDetail(creativeId);
        }
        if (typeof window.loadCreatives === 'function') {
            window.loadCreatives({ silent: true });
        }
    } catch (err) {
        console.warn('reanalyze failed', err);
        alert('Не удалось переанализировать: ' + (err.message || err));
    }
};

/* ---------------------------------------------------------------------------
 * Channel registry — discovered/active channels list inside Settings modal.
 * ------------------------------------------------------------------------- */

window.loadChannelRegistry = async function () {
    const container = document.getElementById('channelRegistryList');
    if (!container) return;
    container.innerHTML = '<div class="loading">Загрузка…</div>';
    try {
        const res = await fetch('/api/channels/registry?limit=200');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const items = Array.isArray(data) ? data : (data.channels || []);
        if (!items.length) {
            container.innerHTML = `
                <div class="muted">
                    Реестр пуст. Запустите автопоиск во вкладке «Фильтры каналов»
                    или добавьте каналы вручную во вкладке «Каналы».
                </div>`;
            return;
        }
        container.innerHTML = `
            <table class="registry-table">
                <thead>
                    <tr>
                        <th>Канал</th>
                        <th>Подписчики</th>
                        <th>ER</th>
                        <th>Реакции</th>
                        <th>Постов/нед</th>
                        <th>Активен</th>
                        <th>Действия</th>
                    </tr>
                </thead>
                <tbody>
                    ${items.map(ch => {
                        const subs = ch.subscribers_count ?? ch.subscribers ?? 0;
                        const er = ch.avg_er ?? 0;
                        const hasReactions = ch.reactions_enabled ?? ch.has_reactions ?? false;
                        const ppw = ch.posts_per_week ?? 0;
                        const active = ch.is_active ?? ch.active ?? false;
                        const sourceLabel = (ch.discovered_via === 'manual' || ch.discovered_via === null)
                            ? '<span class="tag" title="Добавлен вручную">✋ ручной</span>'
                            : '<span class="tag" title="Найден авто-поиском">🔎 авто</span>';
                        // Эвристика «не нашли»: канал есть в реестре, но реальных метрик нет.
                        const looksUnreachable = (Number(subs) === 0) && (Number(er) === 0)
                            && !ch.last_parsed_at && !(ch.posts_collected || 0);
                        const warnBadge = looksUnreachable
                            ? '<span class="tag" title="Канал не удалось найти/обогатить — проверьте алиас, замените или удалите" style="background:#5a2a2a;color:#fbbcbc;">⚠ не найден</span>'
                            : '';
                        return `
                        <tr data-id="${ch.id}" data-username="${window.escapeHtml(ch.username || '')}">
                            <td>
                                <strong>${window.escapeHtml(ch.title || ch.username)}</strong>
                                ${ch.username ? `<div class="muted" style="font-size:12px">@${window.escapeHtml(ch.username)} · ${sourceLabel} ${warnBadge}</div>` : ''}
                            </td>
                            <td>${Number(subs).toLocaleString('ru-RU')}</td>
                            <td>${Number(er).toFixed(1)}%</td>
                            <td>${hasReactions ? '✓' : '—'}</td>
                            <td>${ppw}</td>
                            <td>
                                <label class="switch">
                                    <input type="checkbox" ${active ? 'checked' : ''}
                                           onchange="window.toggleChannel(${ch.id})">
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td style="white-space:nowrap;">
                                <button class="btn btn-secondary btn-sm" type="button"
                                        title="Заменить алиас канала"
                                        onclick="window.renameChannel(${ch.id}, '${window.escapeHtml(ch.username || '')}')">
                                    ✎
                                </button>
                                <button class="btn btn-danger btn-sm" type="button"
                                        title="Удалить канал из реестра"
                                        onclick="window.deleteChannel(${ch.id}, '${window.escapeHtml(ch.username || '')}')">
                                    🗑
                                </button>
                            </td>
                        </tr>`;
                    }).join('')}
                </tbody>
            </table>
        `;
    } catch (err) {
        console.warn('registry load failed', err);
        container.innerHTML = '<div class="muted">Ошибка загрузки реестра</div>';
    }
};

window.toggleChannel = async function (channelId) {
    try {
        const res = await fetch(`/api/channels/registry/${channelId}/toggle`, { method: 'POST' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
    } catch (err) {
        console.warn('toggle failed', err);
        alert('Не удалось переключить канал');
        window.loadChannelRegistry();
    }
};

window.deleteChannel = async function (channelId, username) {
    const label = username ? `@${username}` : `#${channelId}`;
    if (!confirm(`Удалить канал ${label} из реестра?\n\nУже скачанные посты останутся в БД, но канал исчезнет из списков парсинга.`)) {
        return;
    }
    try {
        const res = await fetch(`/api/channels/registry/${channelId}`, { method: 'DELETE' });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || ('HTTP ' + res.status));
        }
        if (typeof showToast === 'function') {
            showToast('info', '✓ Удалено', `Канал ${label} удалён из реестра`);
        }
        window.loadChannelRegistry();
    } catch (err) {
        console.warn('delete channel failed', err);
        alert('Не удалось удалить канал: ' + (err.message || err));
    }
};

window.renameChannel = async function (channelId, currentUsername) {
    const newUsername = prompt(
        `Введите новый username канала (без @).\n\nОставьте поле пустым и нажмите OK, чтобы отменить.\n\nТекущее значение: @${currentUsername || ''}`,
        currentUsername || ''
    );
    if (newUsername === null) return;
    const cleaned = String(newUsername).trim().replace(/^@+/, '');
    if (!cleaned) {
        alert('Имя канала не может быть пустым. Используйте кнопку «🗑», чтобы удалить запись.');
        return;
    }
    if (cleaned.toLowerCase() === (currentUsername || '').toLowerCase()) {
        return;
    }
    try {
        const res = await fetch(`/api/channels/registry/${channelId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: cleaned, title: cleaned }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
        if (typeof showToast === 'function') {
            showToast('info', '✓ Готово',
                `Канал переименован: @${currentUsername} → @${cleaned}. ` +
                'Запустите «Подтянуть статистику», чтобы пересчитать метрики.');
        }
        window.loadChannelRegistry();
    } catch (err) {
        console.warn('rename channel failed', err);
        alert('Не удалось переименовать канал: ' + (err.message || err));
    }
};

/* ---------------------------------------------------------------------------
 * Подтянуть метрики (подписчики/ER/просмотры/постов в неделю) для каналов,
 * у которых статистики нет — дергаем Telethon на бэке.
 * ------------------------------------------------------------------------- */
window.enrichChannels = async function (usernames) {
    const btn = document.getElementById('btnEnrichChannels');
    const label = document.getElementById('enrichStatusLabel');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Запуск…'; }
    try {
        const body = Array.isArray(usernames) && usernames.length
            ? { usernames }
            : {};
        const res = await fetch('/api/channels/enrich', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data.status === 'noop') {
            if (label) label.textContent = 'Все каналы уже с метриками';
            if (btn) { btn.disabled = false; btn.textContent = '📊 Подтянуть статистику'; }
            return;
        }
        if (data.status === 'already_running') {
            if (label) label.textContent = 'Уже выполняется…';
        }
        // Поллинг статуса
        const poll = async () => {
            try {
                const r = await fetch('/api/channels/enrich/status');
                if (!r.ok) throw new Error('HTTP ' + r.status);
                const s = await r.json();
                if (label) {
                    if (s.running) {
                        const cur = s.current ? ` · @${s.current}` : '';
                        label.textContent = `⏳ ${s.done}/${s.total} (✓${s.ok || 0} / ✕${s.failed || 0})${cur}`;
                    } else {
                        label.textContent = s.message || 'Готово';
                    }
                }
                if (s.running) {
                    setTimeout(poll, 1500);
                } else {
                    if (btn) { btn.disabled = false; btn.textContent = '📊 Подтянуть статистику'; }
                    // Если были ошибки — покажем их в alert + консоль, чтобы было видно,
                    // почему не для всех каналов подтянулась статистика.
                    if (Array.isArray(s.errors) && s.errors.length) {
                        const lines = s.errors.slice(0, 20).map(e =>
                            (e && typeof e === 'object')
                                ? `@${e.username}: ${e.reason}`
                                : String(e)
                        );
                        console.warn('enrich errors:', s.errors);
                        const more = s.errors.length > 20 ? `\n…и ещё ${s.errors.length - 20}` : '';
                        alert(
                            `Не подтянулась статистика для ${s.errors.length} канал(ов):\n\n` +
                            lines.join('\n') + more
                        );
                    }
                    window.loadChannelRegistry();
                }
            } catch (e) {
                console.warn('enrich poll failed', e);
                if (btn) { btn.disabled = false; btn.textContent = '📊 Подтянуть статистику'; }
            }
        };
        setTimeout(poll, 800);
    } catch (err) {
        console.warn('enrich failed', err);
        if (label) label.textContent = 'Ошибка запуска обогащения';
        if (btn) { btn.disabled = false; btn.textContent = '📊 Подтянуть статистику'; }
    }
};

/* ---------------------------------------------------------------------------
 * Extended stats — populates the stats badge row + side panel if present.
 * ------------------------------------------------------------------------- */

window.loadExtendedStats = async function () {
    try {
        const res = await fetch('/api/stats/extended');
        if (!res.ok) return;
        const data = await res.json();
        const set = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        set('statApproved', data.approved ?? 0);
        set('statRejected', data.rejected ?? 0);
        set('statPending',  data.pending  ?? 0);
        set('statCitations', data.total_citations ?? 0);
        set('statChannelsActive', data.active_channels ?? 0);
    } catch (err) {
        console.warn('extended stats failed', err);
    }
};

document.addEventListener('DOMContentLoaded', () => {
    // Auto-load registry when its tab is clicked
    const regTab = document.querySelector('.settings-tab[data-tab="registry"]');
    if (regTab) regTab.addEventListener('click', window.loadChannelRegistry);

    // Periodically refresh extended stats (60s)
    window.loadExtendedStats();
    setInterval(window.loadExtendedStats, 60000);
});
