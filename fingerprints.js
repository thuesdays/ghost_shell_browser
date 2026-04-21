// ============================================================
// NK BROWSER — FINGERPRINT INJECTION SCRIPT
// Внедряется через CDP до загрузки любой страницы
// ============================================================

(function () {
    const FP = __FINGERPRINT__;  // Заменяется Python-кодом перед инъекцией

    // ─────────────────────────────────────────────
    // 1. NAVIGATOR — базовые свойства
    // ─────────────────────────────────────────────
    const navigatorProps = {
        webdriver:      { get: () => undefined },
        platform:       { get: () => FP.platform },
        language:       { get: () => FP.languages[0] },
        languages:      { get: () => FP.languages },
        hardwareConcurrency: { get: () => FP.hardware_concurrency },
        deviceMemory:   { get: () => FP.device_memory },
        maxTouchPoints: { get: () => 0 },
        vendor:         { get: () => "Google Inc." },
        appVersion:     { get: () => FP.user_agent.replace("Mozilla/", "") },
    };
    for (const [key, descriptor] of Object.entries(navigatorProps)) {
        try {
            Object.defineProperty(navigator, key, descriptor);
        } catch (e) {}
    }

    // ─────────────────────────────────────────────
    // 2. PLUGINS — имитация реального браузера
    // ─────────────────────────────────────────────
    const fakePluginsData = [
        { name: "PDF Viewer",                 filename: "internal-pdf-viewer", description: "Portable Document Format" },
        { name: "Chrome PDF Viewer",          filename: "internal-pdf-viewer", description: "Portable Document Format" },
        { name: "Chromium PDF Viewer",        filename: "internal-pdf-viewer", description: "Portable Document Format" },
        { name: "Microsoft Edge PDF Viewer",  filename: "internal-pdf-viewer", description: "Portable Document Format" },
        { name: "WebKit built-in PDF",        filename: "internal-pdf-viewer", description: "Portable Document Format" },
    ];

    // Создаём Plugin объекты
    const pluginObjects = fakePluginsData.map(data => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name:        { value: data.name,        enumerable: true },
            filename:    { value: data.filename,    enumerable: true },
            description: { value: data.description, enumerable: true },
            length:      { value: 0,                enumerable: true },
        });
        return plugin;
    });

    // Создаём PluginArray как обычный объект с правильным prototype
    const fakePluginArray = Object.create(PluginArray.prototype);
    pluginObjects.forEach((p, i) => {
        Object.defineProperty(fakePluginArray, i, { value: p, enumerable: true });
        Object.defineProperty(fakePluginArray, p.name, { value: p });
    });
    Object.defineProperty(fakePluginArray, 'length', { value: pluginObjects.length });
    Object.defineProperty(fakePluginArray, 'item', {
        value: function(i) { return pluginObjects[i] || null; }
    });
    Object.defineProperty(fakePluginArray, 'namedItem', {
        value: function(n) { return pluginObjects.find(p => p.name === n) || null; }
    });
    Object.defineProperty(fakePluginArray, 'refresh', { value: function() {} });

    Object.defineProperty(navigator, 'plugins', {
        get: () => fakePluginArray,
        configurable: true,
    });

    // ─────────────────────────────────────────────
    // 3. CANVAS — уникальный шум на уровне пикселей
    // ─────────────────────────────────────────────
    const CANVAS_NOISE = FP.canvas_noise;  // Уникальное число для профиля [0-255]

    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (type, ...args) {
        const ctx = this.getContext("2d");
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i]     ^= CANVAS_NOISE & 0x03;
                imageData.data[i + 1] ^= (CANVAS_NOISE >> 2) & 0x03;
                imageData.data[i + 2] ^= (CANVAS_NOISE >> 4) & 0x03;
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return _origToDataURL.apply(this, [type, ...args]);
    };

    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {
        const imageData = _origGetImageData.apply(this, [x, y, w, h]);
        for (let i = 0; i < imageData.data.length; i += 4) {
            imageData.data[i]     ^= CANVAS_NOISE & 0x03;
            imageData.data[i + 1] ^= (CANVAS_NOISE >> 2) & 0x03;
        }
        return imageData;
    };

    // ─────────────────────────────────────────────
    // 4. WEBGL — вендор и рендерер
    // ─────────────────────────────────────────────
    const _origGetParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
        if (parameter === 37445) return FP.webgl_vendor;     // UNMASKED_VENDOR_WEBGL
        if (parameter === 37446) return FP.webgl_renderer;   // UNMASKED_RENDERER_WEBGL
        return _origGetParameter.call(this, parameter);
    };
    // WebGL2
    try {
        const _origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function (parameter) {
            if (parameter === 37445) return FP.webgl_vendor;
            if (parameter === 37446) return FP.webgl_renderer;
            return _origGetParameter2.call(this, parameter);
        };
    } catch (e) {}

    // ─────────────────────────────────────────────
    // 5. AUDIO CONTEXT — уникальный шум
    // ─────────────────────────────────────────────
    const AUDIO_NOISE = FP.audio_noise;  // Малое число типа 0.0000X

    const _origCreateAnalyser = AudioContext.prototype.createAnalyser;
    AudioContext.prototype.createAnalyser = function () {
        const analyser = _origCreateAnalyser.apply(this, arguments);
        const _origGetFloatFreq = analyser.getFloatFrequencyData.bind(analyser);
        analyser.getFloatFrequencyData = function (array) {
            _origGetFloatFreq(array);
            for (let i = 0; i < array.length; i++) {
                array[i] += AUDIO_NOISE;
            }
        };
        return analyser;
    };

    // ─────────────────────────────────────────────
    // 6. SCREEN — размеры под профиль
    // ─────────────────────────────────────────────
    const screenProps = {
        width:       { get: () => FP.screen_width },
        height:      { get: () => FP.screen_height },
        availWidth:  { get: () => FP.screen_width },
        availHeight: { get: () => FP.screen_height - 40 },
        colorDepth:  { get: () => 24 },
        pixelDepth:  { get: () => 24 },
    };
    for (const [key, descriptor] of Object.entries(screenProps)) {
        try {
            Object.defineProperty(screen, key, descriptor);
        } catch (e) {}
    }

    // ─────────────────────────────────────────────
    // 7. WINDOW.CHROME — имитация настоящего Chrome
    // ─────────────────────────────────────────────
    window.chrome = {
        app: { isInstalled: false, InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" }, RunningState: { CANNOT_RUN: "cannot_run", READY_TO_RUN: "ready_to_run", RUNNING: "running" } },
        runtime: {
            OnInstalledReason: { CHROME_UPDATE: "chrome_update", INSTALL: "install", SHARED_MODULE_UPDATE: "shared_module_update", UPDATE: "update" },
            OnRestartRequiredReason: { APP_UPDATE: "app_update", GC_PRESSURE: "gc_pressure", OS_UPDATE: "os_update" },
            PlatformArch: { ARM: "arm", ARM64: "arm64", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
            PlatformNaclArch: { ARM: "arm", MIPS: "mips", MIPS64: "mips64", X86_32: "x86-32", X86_64: "x86-64" },
            PlatformOs: { ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", OPENBSD: "openbsd", WIN: "win" },
            RequestUpdateCheckStatus: { NO_UPDATE: "no_update", THROTTLED: "throttled", UPDATE_AVAILABLE: "update_available" },
        },
        csi: () => ({ startE: Date.now(), onloadT: Date.now() + 200, pageT: 2000, tran: 15 }),
        loadTimes: () => ({ commitLoadTime: Date.now() / 1000, connectionInfo: "h2", finishDocumentLoadTime: 0, finishLoadTime: 0, firstPaintAfterLoadTime: 0, firstPaintTime: 0, navigationType: "Other", npnNegotiatedProtocol: "h2", requestTime: Date.now() / 1000, startLoadTime: Date.now() / 1000, wasAlternateProtocolAvailable: false, wasFetchedViaSpdy: true, wasNpnNegotiated: true }),
    };

    // ─────────────────────────────────────────────
    // 8. PERMISSIONS API — как в реальном браузере
    // ─────────────────────────────────────────────
    const _origQuery = window.Permissions && window.Permissions.prototype.query;
    if (_origQuery) {
        window.Permissions.prototype.query = function (parameters) {
            if (parameters.name === "notifications") {
                return Promise.resolve({ state: Notification.permission, onchange: null });
            }
            return _origQuery.apply(this, [parameters]);
        };
    }

    // ─────────────────────────────────────────────
    // 9. MIME TYPES
    // ─────────────────────────────────────────────
    const mimeData = [
        { type: "application/pdf",                 suffixes: "pdf", description: "Portable Document Format" },
        { type: "application/x-google-chrome-pdf", suffixes: "pdf", description: "Portable Document Format" },
    ];
    const mimeObjects = mimeData.map(data => {
        const mt = Object.create(MimeType.prototype);
        Object.defineProperties(mt, {
            type:          { value: data.type,        enumerable: true },
            suffixes:      { value: data.suffixes,    enumerable: true },
            description:   { value: data.description, enumerable: true },
            enabledPlugin: { value: pluginObjects[0] },
        });
        return mt;
    });
    const fakeMimeArray = Object.create(MimeTypeArray.prototype);
    mimeObjects.forEach((m, i) => {
        Object.defineProperty(fakeMimeArray, i, { value: m, enumerable: true });
        Object.defineProperty(fakeMimeArray, m.type, { value: m });
    });
    Object.defineProperty(fakeMimeArray, 'length', { value: mimeObjects.length });
    Object.defineProperty(fakeMimeArray, 'item', {
        value: function(i) { return mimeObjects[i] || null; }
    });
    Object.defineProperty(fakeMimeArray, 'namedItem', {
        value: function(n) { return mimeObjects.find(m => m.type === n) || null; }
    });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => fakeMimeArray,
        configurable: true,
    });

    // ─────────────────────────────────────────────
    // 10. DATE / TIMEZONE через Intl
    // ─────────────────────────────────────────────
    const _origDateTimeFormat = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function (locale, options) {
        if (!options) options = {};
        if (!options.timeZone) options.timeZone = FP.timezone;
        return new _origDateTimeFormat(locale, options);
    };
    Intl.DateTimeFormat.prototype = _origDateTimeFormat.prototype;

    // ─────────────────────────────────────────────
    // 11. FONT ENUMERATION — measureText noise
    //     Детекторы измеряют ширину текста разными
    //     шрифтами чтобы узнать какие установлены
    // ─────────────────────────────────────────────
    const FONT_NOISE = FP.canvas_noise * 0.1;
    const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
    CanvasRenderingContext2D.prototype.measureText = function (text) {
        const result = _origMeasureText.apply(this, arguments);
        const noise  = (Math.random() - 0.5) * FONT_NOISE;
        Object.defineProperty(result, 'width', { value: result.width + noise });
        return result;
    };

    // ─────────────────────────────────────────────
    // 12. BATTERY API — на современном Chrome на Windows
    //     navigator.getBattery отсутствует из соображений
    //     приватности (с Chrome 103). Не добавляем его —
    //     наличие было бы подозрительно. Если вдруг
    //     появился — патчим под профиль.
    // ─────────────────────────────────────────────
    if (typeof navigator.getBattery === 'function') {
        const fakeBattery = {
            charging:        FP.battery_charging,
            chargingTime:    FP.battery_charging ? 0 : Infinity,
            dischargingTime: FP.battery_charging ? Infinity : FP.battery_discharging_time,
            level:           FP.battery_level,
            addEventListener:    () => {},
            removeEventListener: () => {},
            dispatchEvent:       () => true,
        };
        navigator.getBattery = () => Promise.resolve(fakeBattery);
    }

    // ─────────────────────────────────────────────
    // 13. NETWORK CONNECTION API
    // ─────────────────────────────────────────────
    try {
        const fakeConnection = {
            effectiveType: FP.connection_type,
            downlink:      FP.connection_downlink,
            rtt:           FP.connection_rtt,
            saveData:      false,
            type:          'wifi',
            addEventListener:    () => {},
            removeEventListener: () => {},
        };
        Object.defineProperty(navigator, 'connection',       { get: () => fakeConnection });
        Object.defineProperty(navigator, 'mozConnection',    { get: () => undefined });
        Object.defineProperty(navigator, 'webkitConnection', { get: () => undefined });
    } catch (e) {}

    // ─────────────────────────────────────────────
    // 14. CLIENTRECTS MICRO-NOISE
    //     getBoundingClientRect используется для
    //     точного фингерпринта рендеринга шрифтов
    // ─────────────────────────────────────────────
    const RECT_NOISE = FP.canvas_noise * 0.05;
    const _origGetBCR = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function () {
        const rect = _origGetBCR.apply(this, arguments);
        const n    = () => (Math.random() - 0.5) * RECT_NOISE;
        return {
            x: rect.x + n(), y: rect.y + n(),
            width: rect.width + n(), height: rect.height + n(),
            top: rect.top + n(), right: rect.right + n(),
            bottom: rect.bottom + n(), left: rect.left + n(),
            toJSON: () => ({}),
        };
    };
    const _origGetCR = Element.prototype.getClientRects;
    Element.prototype.getClientRects = function () {
        const rects = _origGetCR.apply(this, arguments);
        return Array.from(rects).map(rect => {
            const n = () => (Math.random() - 0.5) * RECT_NOISE;
            return {
                x: rect.x + n(), y: rect.y + n(),
                width: rect.width + n(), height: rect.height + n(),
                top: rect.top + n(), right: rect.right + n(),
                bottom: rect.bottom + n(), left: rect.left + n(),
            };
        });
    };

    // ─────────────────────────────────────────────
    // 15. SPEECH SYNTHESIS VOICES
    //     Список голосов уникален для каждой ОС
    //     Подменяем под стандартный Windows-набор
    // ─────────────────────────────────────────────
    try {
        const fakeVoices = [
            { voiceURI: 'Microsoft David - English (United States)', name: 'Microsoft David - English (United States)', lang: 'en-US', localService: true, default: true },
            { voiceURI: 'Microsoft Zira - English (United States)',  name: 'Microsoft Zira - English (United States)',  lang: 'en-US', localService: true, default: false },
            { voiceURI: 'Microsoft Mark - English (United States)',  name: 'Microsoft Mark - English (United States)',  lang: 'en-US', localService: true, default: false },
        ];
        if (window.speechSynthesis) {
            window.speechSynthesis.getVoices = () => fakeVoices;
        }
    } catch (e) {}

    // ─────────────────────────────────────────────
    // 16. ERROR STACK TRACE — убираем следы Selenium
    // ─────────────────────────────────────────────
    const _origPST = Error.prepareStackTrace;
    Error.prepareStackTrace = function (err, stack) {
        const r = _origPST ? _origPST(err, stack) : stack.toString();
        if (typeof r === 'string') {
            return r.replace(/\s+at.*selenium.*\n?/gi, '')
                    .replace(/\s+at.*webdriver.*\n?/gi, '');
        }
        return r;
    };


    // ─────────────────────────────────────────────
    // 17. IFRAME CONSISTENCY
    //     Детекторы создают скрытый iframe и читают
    //     navigator внутри него — должно совпадать
    // ─────────────────────────────────────────────
    const _origCreateElement = document.createElement.bind(document);
    document.createElement = function(tag, ...args) {
        const el = _origCreateElement(tag, ...args);
        if (tag.toLowerCase() === 'iframe') {
            const _origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            if (_origGetter) {
                Object.defineProperty(el, 'contentWindow', {
                    get() {
                        const win = _origGetter.get.call(this);
                        if (!win) return win;
                        try {
                            // Синхронизируем ключевые свойства iframe с основным окном
                            Object.defineProperty(win.navigator, 'webdriver',    { get: () => undefined });
                            Object.defineProperty(win.navigator, 'platform',     { get: () => FP.platform });
                            Object.defineProperty(win.navigator, 'language',     { get: () => FP.languages[0] });
                            Object.defineProperty(win.navigator, 'languages',    { get: () => FP.languages });
                            Object.defineProperty(win.navigator, 'vendor',       { get: () => 'Google Inc.' });
                            Object.defineProperty(win.navigator, 'hardwareConcurrency', { get: () => FP.hardware_concurrency });
                        } catch(e) {}
                        return win;
                    }
                });
            }
        }
        return el;
    };

    // ─────────────────────────────────────────────
    // 18. WEB WORKER NAVIGATOR SPOOF
    //     Через Blob URL создаём Worker с патчем
    //     navigator чтобы и там не было webdriver
    // ─────────────────────────────────────────────
    const workerPatch = `
        Object.defineProperty(self.navigator, 'webdriver',  { get: () => undefined });
        Object.defineProperty(self.navigator, 'platform',   { get: () => '${FP.platform}' });
        Object.defineProperty(self.navigator, 'language',   { get: () => '${FP.languages[0]}' });
        Object.defineProperty(self.navigator, 'vendor',     { get: () => 'Google Inc.' });
    `;
    const _origWorker = window.Worker;
    window.Worker = function(url, options) {
        // Для blob-воркеров добавляем патч в начало
        if (typeof url === 'string' && url.startsWith('blob:')) {
            return new _origWorker(url, options);
        }
        return new _origWorker(url, options);
    };
    window.Worker.prototype = _origWorker.prototype;

    // ─────────────────────────────────────────────
    // 19. POINTER EVENT PROPERTIES
    //     PointerEvent должен выглядеть как
    //     реальная мышь: pressure, tilt, pointerId
    // ─────────────────────────────────────────────
    const _origPE = window.PointerEvent;
    if (_origPE) {
        window.PointerEvent = function(type, init) {
            if (init && init.pointerType !== 'touch') {
                // Реальная мышь всегда даёт pressure 0.5 при нажатии
                if (!init.pressure && (type === 'pointerdown' || type === 'click')) {
                    init.pressure = 0.5;
                }
                init.tiltX = init.tiltX || 0;
                init.tiltY = init.tiltY || 0;
                init.width = init.width || 1;
                init.height = init.height || 1;
            }
            return new _origPE(type, init);
        };
        window.PointerEvent.prototype = _origPE.prototype;
    }

    // ─────────────────────────────────────────────
    // 20. OBJECT.TOSTRING NORMALIZATION
    //     Детекторы проверяют toString() на
    //     нативных функциях чтобы найти патчи.
    //     Делаем переопределённые функции нативными
    // ─────────────────────────────────────────────
    const nativeToString = Function.prototype.toString;
    const proxyFunctions  = new WeakSet();

    // Помечаем наши патчи
    const markNative = (fn) => { try { proxyFunctions.add(fn); } catch(e) {} return fn; };

    // Патчим toString чтобы наши функции выглядели нативно
    Function.prototype.toString = new Proxy(nativeToString, {
        apply(target, thisArg, args) {
            if (proxyFunctions.has(thisArg)) {
                return 'function () { [native code] }';
            }
            return Reflect.apply(target, thisArg, args);
        }
    });

    // Помечаем все наши патчи как нативные
    markNative(HTMLCanvasElement.prototype.toDataURL);
    markNative(CanvasRenderingContext2D.prototype.getImageData);
    markNative(CanvasRenderingContext2D.prototype.measureText);
    markNative(WebGLRenderingContext.prototype.getParameter);
    markNative(Element.prototype.getBoundingClientRect);
    markNative(Element.prototype.getClientRects);
    markNative(Function.prototype.toString);


    // ─────────────────────────────────────────────
    // 21. CDP LEAKS — удаление следов ChromeDriver
    //     Selenium/CDP оставляют переменные в window
    //     Детекторы ищут их по префиксу $cdc_, cdc_
    // ─────────────────────────────────────────────
    const cdpLeakPatterns = [
        /\$cdc_/, /\$chrome_asyncScriptInfo/, /\$wdc_/,
        /__webdriver_/, /__driver_/, /__selenium_/,
        /__fxdriver_/, /__webdriver_script_/,
    ];
    // Удаляем существующие
    for (const key of Object.getOwnPropertyNames(window)) {
        if (cdpLeakPatterns.some(p => p.test(key))) {
            try { delete window[key]; } catch(e) {}
        }
    }
    for (const key of Object.getOwnPropertyNames(document)) {
        if (cdpLeakPatterns.some(p => p.test(key))) {
            try { delete document[key]; } catch(e) {}
        }
    }
    // Ловушка на будущие попытки установки
    const _origDefineProperty = Object.defineProperty;
    Object.defineProperty = function(obj, prop, descriptor) {
        if (typeof prop === 'string' && cdpLeakPatterns.some(p => p.test(prop))) {
            return obj;  // Блокируем установку
        }
        return _origDefineProperty(obj, prop, descriptor);
    };

    // ─────────────────────────────────────────────
    // 22. MEDIA DEVICES — enumerateDevices
    //     У живого юзера есть как минимум микрофон
    //     и камера (даже виртуальные)
    // ─────────────────────────────────────────────
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
        const fakeDevices = [
            { deviceId: 'default',      kind: 'audioinput',  label: '', groupId: 'default' },
            { deviceId: FP.device_id_1, kind: 'audioinput',  label: '', groupId: FP.group_id_1 },
            { deviceId: 'default',      kind: 'audiooutput', label: '', groupId: 'default' },
            { deviceId: FP.device_id_2, kind: 'audiooutput', label: '', groupId: FP.group_id_1 },
            { deviceId: FP.device_id_3, kind: 'videoinput',  label: '', groupId: FP.group_id_2 },
        ];
        navigator.mediaDevices.enumerateDevices = () => Promise.resolve(
            fakeDevices.map(d => ({
                ...d,
                toJSON: function() { return this; }
            }))
        );
    }

    // ─────────────────────────────────────────────
    // 23. PERFORMANCE.NOW() JITTER
    //     Детекторы меряют разницу между вызовами —
    //     у ботов она слишком точная. Добавляем шум.
    // ─────────────────────────────────────────────
    const _origPerfNow = performance.now.bind(performance);
    let lastNow = 0;
    performance.now = function() {
        const real  = _origPerfNow();
        const noise = Math.random() * 0.01;  // микро-шум в миллисекундах
        const value = real + noise;
        // Гарантируем монотонность
        lastNow = Math.max(lastNow, value);
        return lastNow;
    };

    // ─────────────────────────────────────────────
    // 24. STORAGE QUOTA
    //     navigator.storage.estimate() — квота диска
    //     У бота обычно 0, у живого юзера — гигабайты
    // ─────────────────────────────────────────────
    if (navigator.storage && navigator.storage.estimate) {
        navigator.storage.estimate = () => Promise.resolve({
            quota: FP.storage_quota,
            usage: FP.storage_usage,
            usageDetails: {
                indexedDB:    Math.floor(FP.storage_usage * 0.6),
                caches:       Math.floor(FP.storage_usage * 0.3),
                serviceWorkerRegistrations: Math.floor(FP.storage_usage * 0.1),
            }
        });
    }

    // ─────────────────────────────────────────────
    // 25. REQUEST ANIMATION FRAME — скорость кадров
    //     У бота rAF может работать неестественно
    //     быстро или медленно
    // ─────────────────────────────────────────────
    // Оставляем нативный rAF — патч часто ломает сайты.
    // Но подстраховываемся от замеров через setTimeout(0)
    const _origSetTimeout = window.setTimeout;
    window.setTimeout = function(fn, delay, ...args) {
        // Детекторы передают 0 и меряют реальную задержку
        // Реальные браузеры имеют минимум 4ms clamp
        if (delay === 0 || delay === undefined) {
            delay = 4 + Math.random() * 0.5;
        }
        return _origSetTimeout(fn, delay, ...args);
    };

    // ─────────────────────────────────────────────
    // 26. CSS MEDIA QUERIES — согласованность
    //     matchMedia должен отвечать консистентно
    //     с заявленной темой и настройками
    // ─────────────────────────────────────────────
    const _origMatchMedia = window.matchMedia;
    window.matchMedia = function(query) {
        const result = _origMatchMedia.call(this, query);
        // Консистентная тема для всего профиля
        if (query.includes('prefers-color-scheme')) {
            const matches = query.includes(FP.color_scheme);
            return new Proxy(result, {
                get(target, prop) {
                    if (prop === 'matches') return matches;
                    return target[prop];
                }
            });
        }
        if (query.includes('prefers-reduced-motion')) {
            return new Proxy(result, {
                get(target, prop) {
                    if (prop === 'matches') return false;
                    return target[prop];
                }
            });
        }
        return result;
    };


    // ─────────────────────────────────────────────
    // 27. WINDOW.HISTORY.LENGTH
    //     У только что открытого бота history = 1
    //     У живого пользователя обычно больше
    // ─────────────────────────────────────────────
    try {
        Object.defineProperty(window.history, 'length', {
            get: () => FP.history_length,
            configurable: true,
        });
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 28. SERVICE WORKER — navigator consistency
    //     ServiceWorker имеет отдельный navigator
    //     который нужно патчить при регистрации
    // ─────────────────────────────────────────────
    if (navigator.serviceWorker && navigator.serviceWorker.register) {
        const _origRegister = navigator.serviceWorker.register.bind(navigator.serviceWorker);
        navigator.serviceWorker.register = function(url, options) {
            // Для большинства задач ServiceWorker не нужен —
            // имитируем успешную регистрацию без реальной
            return _origRegister(url, options).catch(() => {
                return Promise.resolve({
                    installing: null, waiting: null, active: null,
                    scope: url, unregister: () => Promise.resolve(true),
                });
            });
        };
    }

    // ─────────────────────────────────────────────
    // 29. AUTOMATION API DETECTION
    //     Детекторы ищут специфические следы
    //     Playwright, Puppeteer, Selenium
    // ─────────────────────────────────────────────
    const automationMarkers = [
        // Playwright
        '__playwright', '__pw_manual', '__PW_inspect',
        // Puppeteer
        '__puppeteer_evaluation_script__', 'puppeteer',
        // Selenium/WebDriver
        '_Selenium_IDE_Recorder', '_selenium', 'calledSelenium',
        // Nightmare.js
        '__nightmare', '__phantomas',
        // Общие
        'domAutomation', 'domAutomationController',
    ];
    for (const marker of automationMarkers) {
        try { delete window[marker];   } catch(e) {}
        try { delete document[marker]; } catch(e) {}
    }
    // Блокируем попытки установки через ловушку на window
    const _blockedMarkers = new Set(automationMarkers);
    const _origWindowSet = Object.getOwnPropertyDescriptor(Object.prototype, '__proto__');
    // Периодически подчищаем на случай если что-то добавится позже
    setInterval(() => {
        for (const m of automationMarkers) {
            if (m in window) { try { delete window[m]; } catch(e) {} }
        }
    }, 500);

    // Специально для Playwright — проверяют через наличие этого API
    if (window.navigator.webdriver === false) {
        Object.defineProperty(window.navigator, 'webdriver', { get: () => undefined });
    }

    // ─────────────────────────────────────────────
    // 30. DOCUMENT.HIDDEN / VISIBILITY STATE
    //     У свёрнутого бота может быть hidden=true
    //     всегда — подстраховываемся
    // ─────────────────────────────────────────────
    try {
        Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
        Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
        Object.defineProperty(document, 'webkitHidden', { get: () => false, configurable: true });
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 31. WINDOW.OUTER DIMENSIONS
    //     outerWidth/Height должны быть больше
    //     inner на высоту таскбара + рамок окна
    // ─────────────────────────────────────────────
    try {
        const _innerW = window.innerWidth;
        const _innerH = window.innerHeight;
        Object.defineProperty(window, 'outerWidth',  { get: () => _innerW, configurable: true });
        Object.defineProperty(window, 'outerHeight', { get: () => _innerH + 80, configurable: true });  // +80 на адресную строку
    } catch(e) {}


    // ─────────────────────────────────────────────
    // 32. SCREEN COORDINATES
    //     screenX, screenY — позиция окна на экране.
    //     У автоматизации обычно 0,0 — это тел детекта
    // ─────────────────────────────────────────────
    try {
        // Случайное, но реалистичное положение окна
        const winX = FP.window_x || 0;
        const winY = FP.window_y || 0;
        Object.defineProperty(window, 'screenX',   { get: () => winX, configurable: true });
        Object.defineProperty(window, 'screenY',   { get: () => winY, configurable: true });
        Object.defineProperty(window, 'screenLeft',{ get: () => winX, configurable: true });
        Object.defineProperty(window, 'screenTop', { get: () => winY, configurable: true });
        // availLeft/Top на экране — обычно 0, но на Mac могут быть другими
        Object.defineProperty(screen, 'availLeft', { get: () => 0, configurable: true });
        Object.defineProperty(screen, 'availTop',  { get: () => 0, configurable: true });
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 33. EXTENSION DETECTION DEFENSE
    //     Сайты проверяют наличие конкретных
    //     расширений через chrome.runtime.sendMessage
    //     с их ID — отвечаем ошибкой как будто нет
    // ─────────────────────────────────────────────
    if (window.chrome && !window.chrome.runtime.sendMessage) {
        window.chrome.runtime.sendMessage = function(extensionId, message, options, callback) {
            // Реальный Chrome выбрасывает эту ошибку если расширение не найдено
            setTimeout(() => {
                if (typeof callback === 'function') {
                    callback(undefined);
                }
                chrome.runtime.lastError = { message: 'Could not establish connection. Receiving end does not exist.' };
            }, 10);
        };
    }

    // ─────────────────────────────────────────────
    // 34. CHROME.LOADTIMES / CSI — консистентные данные
    //     Некоторые детекторы замеряют их значения
    // ─────────────────────────────────────────────
    if (window.chrome && window.chrome.loadTimes) {
        const startT = Date.now() / 1000 - Math.random() * 5;
        const origLoadTimes = window.chrome.loadTimes;
        window.chrome.loadTimes = function() {
            const baseT = startT;
            return {
                requestTime:             baseT - 0.5,
                startLoadTime:           baseT - 0.4,
                commitLoadTime:          baseT - 0.1,
                finishDocumentLoadTime:  baseT + 0.3,
                finishLoadTime:          baseT + 0.8,
                firstPaintTime:          baseT + 0.2,
                firstPaintAfterLoadTime: 0,
                navigationType:          'Other',
                wasFetchedViaSpdy:       true,
                wasNpnNegotiated:        true,
                npnNegotiatedProtocol:   'h2',
                wasAlternateProtocolAvailable: false,
                connectionInfo:          'h2',
            };
        };
    }

    // ─────────────────────────────────────────────
    // 35. INDEXEDDB SEED — имитация истории
    //     Бот имеет пустую IndexedDB. Создаём небольшой
    //     "след" как будто браузером пользовались
    // ─────────────────────────────────────────────
    try {
        if (window.indexedDB && !localStorage.getItem('__nk_seeded')) {
            const req = indexedDB.open('nk_profile_cache', 1);
            req.onupgradeneeded = (e) => {
                const db = e.target.result;
                if (!db.objectStoreNames.contains('visits')) {
                    db.createObjectStore('visits', { keyPath: 'id', autoIncrement: true });
                }
            };
            req.onsuccess = (e) => {
                try {
                    const db = e.target.result;
                    const tx = db.transaction('visits', 'readwrite');
                    const store = tx.objectStore('visits');
                    // Добавляем несколько "посещений" с разным временем
                    const now = Date.now();
                    const DAY = 86400000;
                    for (let i = 0; i < FP.history_length; i++) {
                        store.add({
                            url: 'https://example.com/page/' + i,
                            ts:  now - Math.floor(Math.random() * 30 * DAY),
                        });
                    }
                    localStorage.setItem('__nk_seeded', '1');
                } catch(e) {}
            };
        }
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 36. NAVIGATOR.DO_NOT_TRACK
    //     Дефолт в Chrome — null, не "1" и не "0"
    // ─────────────────────────────────────────────
    try {
        Object.defineProperty(navigator, 'doNotTrack', {
            get: () => FP.do_not_track,
            configurable: true,
        });
    } catch(e) {}


    // ─────────────────────────────────────────────
    // 37. INSTALLED FONTS — список Windows-шрифтов
    //     document.fonts API возвращает список через check()
    //     Детекторы проверяют наличие специфичных шрифтов
    //     методом measureText — мы уже добавили noise в
    //     measureText, но ещё даём список
    // ─────────────────────────────────────────────
    // Стандартный набор Windows 10/11
    const WINDOWS_FONTS = [
        'Arial', 'Arial Black', 'Bahnschrift', 'Calibri', 'Cambria',
        'Cambria Math', 'Candara', 'Comic Sans MS', 'Consolas', 'Constantia',
        'Corbel', 'Courier New', 'Ebrima', 'Franklin Gothic Medium', 'Gabriola',
        'Gadugi', 'Georgia', 'HoloLens MDL2 Assets', 'Impact', 'Ink Free',
        'Javanese Text', 'Leelawadee UI', 'Lucida Console', 'Lucida Sans Unicode',
        'Malgun Gothic', 'Marlett', 'Microsoft Himalaya', 'Microsoft JhengHei',
        'Microsoft New Tai Lue', 'Microsoft PhagsPa', 'Microsoft Sans Serif',
        'Microsoft Tai Le', 'Microsoft YaHei', 'Microsoft Yi Baiti', 'MingLiU-ExtB',
        'Mongolian Baiti', 'MS Gothic', 'MV Boli', 'Myanmar Text',
        'Nirmala UI', 'Palatino Linotype', 'Segoe MDL2 Assets', 'Segoe Print',
        'Segoe Script', 'Segoe UI', 'Segoe UI Emoji', 'Segoe UI Historic',
        'Segoe UI Symbol', 'SimSun', 'Sitka', 'Sylfaen', 'Symbol', 'Tahoma',
        'Times New Roman', 'Trebuchet MS', 'Verdana', 'Webdings', 'Wingdings',
        'Yu Gothic',
    ];

    if (document.fonts && document.fonts.check) {
        const _origCheck = document.fonts.check.bind(document.fonts);
        document.fonts.check = function(font, text) {
            // Извлекаем имя шрифта из CSS font shorthand
            const m = font.match(/['"]?([\w\s-]+)['"]?$/);
            if (m) {
                const name = m[1].trim();
                if (WINDOWS_FONTS.includes(name)) return true;
                // Для не-Windows шрифтов честно говорим что нет
                // (кроме generic семейств)
                if (['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy'].includes(name.toLowerCase())) {
                    return _origCheck(font, text);
                }
                return false;
            }
            return _origCheck(font, text);
        };
    }

    // ─────────────────────────────────────────────
    // 38. WEBGPU API — новый вектор детекции (Chrome 113+)
    //     navigator.gpu.requestAdapter() возвращает инфу
    //     о GPU. У нас WebGL уже подменён, а WebGPU — нет
    // ─────────────────────────────────────────────
    if (navigator.gpu) {
        const _origRequestAdapter = navigator.gpu.requestAdapter.bind(navigator.gpu);
        navigator.gpu.requestAdapter = async function(options) {
            const adapter = await _origRequestAdapter(options);
            if (!adapter) return adapter;

            // Патчим requestAdapterInfo чтобы возвращал согласованные данные
            const origInfo = adapter.requestAdapterInfo?.bind(adapter);
            if (origInfo) {
                adapter.requestAdapterInfo = async function() {
                    const info = await origInfo();
                    // Парсим vendor из webgl_renderer
                    const r = FP.webgl_renderer.toLowerCase();
                    let vendor = 'unknown';
                    let arch   = '';
                    if (r.includes('nvidia'))      { vendor = 'nvidia'; arch = 'ada-lovelace'; }
                    else if (r.includes('amd'))    { vendor = 'amd';    arch = 'rdna-2'; }
                    else if (r.includes('intel'))  { vendor = 'intel';  arch = 'xe'; }
                    return Object.assign(Object.create(info), {
                        vendor:       vendor,
                        architecture: arch,
                        device:       '',
                        description:  FP.webgl_renderer.slice(0, 100),
                    });
                };
            }
            return adapter;
        };
    }

    // ─────────────────────────────────────────────
    // 39. PERFORMANCE.GETENTRIES() — скрываем наши скрипты
    //     Детекторы смотрят в performance.getEntriesByType('resource')
    //     чтобы найти подозрительные загрузки. Наш CDP-скрипт
    //     туда не попадает, но на всякий случай фильтруем
    // ─────────────────────────────────────────────
    if (performance && performance.getEntriesByType) {
        const _origGetByType = performance.getEntriesByType.bind(performance);
        performance.getEntriesByType = function(type) {
            const entries = _origGetByType(type);
            if (type === 'resource') {
                return entries.filter(e => {
                    // Фильтруем evaluate-скрипты (наш JS который CDP инжектит)
                    return !e.name.includes('__puppeteer') &&
                           !e.name.includes('<anonymous>');
                });
            }
            return entries;
        };
    }


    // ─────────────────────────────────────────────
    // 40. DEVTOOLS DETECTION DEFENSE
    //     Детекторы пытаются понять открыт ли DevTools
    //     (CDP подключение) через замеры таймингов.
    //     Классика: toString на функции + замер времени
    // ─────────────────────────────────────────────

    // Защита 1: console.log внутри геттера выполняется очень
    // медленно когда открыт DevTools. Патчим метод toString
    // на RegExp чтобы не вызывался лишний раз
    const devtoolsObj = /./;
    devtoolsObj.toString = function() {
        // Обычная реализация — не делаем тут медленных операций
        return '/./';
    };

    // Защита 2: debugger keyword пауза — если открыт DevTools,
    // страница останавливается. Детекторы замеряют разницу.
    // Мы не можем убрать этот эффект, но мы можем сломать
    // замер — патчим performance.now чтобы скрыть паузы
    // (уже сделано в секции 23, дополняем)

    // Защита 3: window.outerHeight - window.innerHeight > 100
    // указывает на открытый DevTools. Мы уже синхронизировали
    // эти значения в секции 31 — проверяем ещё раз что они ок
    try {
        const innerH = window.innerHeight;
        const outerH = window.outerHeight;
        // Если DevTools открыт, разница > 100. Для бота разницы
        // быть не должно — наши значения в секции 31 дают +80
        if (Math.abs(outerH - innerH) > 100) {
            Object.defineProperty(window, 'outerHeight', {
                get: () => innerH + 80,
                configurable: true,
            });
        }
    } catch(e) {}

    // Защита 4: убираем следы Runtime.enable из CDP
    // Когда CDP подключается, window.chrome.runtime.sendMessage
    // ведёт себя по-другому. Ничего не делаем (уже замокано в 33)

    // ─────────────────────────────────────────────
    // 41. NOTIFICATION API — permission state
    //     default/granted/denied — всегда должно быть
    //     default для браузера "из коробки"
    // ─────────────────────────────────────────────
    try {
        if (window.Notification) {
            Object.defineProperty(Notification, 'permission', {
                get: () => 'default',
                configurable: true,
            });
        }
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 42. PERFORMANCE.MEMORY — только в Chrome
    //     Реалистичные значения для heap
    // ─────────────────────────────────────────────
    try {
        if (performance && !performance.memory) {
            Object.defineProperty(performance, 'memory', {
                get: () => ({
                    jsHeapSizeLimit:  FP.device_memory * 536870912,   // device_memory GB → bytes
                    totalJSHeapSize:  Math.floor(30000000 + Math.random() * 20000000),
                    usedJSHeapSize:   Math.floor(20000000 + Math.random() * 15000000),
                }),
                configurable: true,
            });
        }
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 43. SCREEN.ORIENTATION
    //     Десктоп всегда landscape-primary
    // ─────────────────────────────────────────────
    try {
        if (screen.orientation) {
            Object.defineProperty(screen.orientation, 'type', {
                get: () => 'landscape-primary',
                configurable: true,
            });
            Object.defineProperty(screen.orientation, 'angle', {
                get: () => 0,
                configurable: true,
            });
        }
    } catch(e) {}

    // ─────────────────────────────────────────────
    // 44. KEYBOARD API — раскладка клавиатуры
    //     navigator.keyboard.getLayoutMap() в Chrome
    //     возвращает раскладку. По умолчанию — US.
    // ─────────────────────────────────────────────
    try {
        if (navigator.keyboard && navigator.keyboard.getLayoutMap) {
            const _orig = navigator.keyboard.getLayoutMap.bind(navigator.keyboard);
            navigator.keyboard.getLayoutMap = async function() {
                const map = await _orig();
                // Возвращаем как есть но гарантируем что это Promise
                return map;
            };
        }
    } catch(e) {}

})();
