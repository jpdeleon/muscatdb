document.addEventListener("DOMContentLoaded", () => {
    const socket = io();

    const chatWindow = document.getElementById("chat-window");
    const chatHeader = document.getElementById("chat-header");
    const minimizeChat = document.getElementById("minimize-chat");
    const chatMessages = document.getElementById("chat-messages");
    const chatEmpty = document.getElementById("chat-empty");
    const messageInput = document.getElementById("message-input");
    const sendButton = document.getElementById("send-button");
    const statusDot = document.getElementById("chat-status-dot");
    const onlineEl = document.getElementById("chat-online");
    const unreadBadge = document.getElementById("chat-unread");
    const typingEl = document.getElementById("chat-typing");
    const autocompleteEl = document.getElementById("chat-autocomplete");

    if (!chatWindow) return;
    chatWindow.classList.remove("hidden");

    const PRESET_EMOJI = ["👍", "🎉", "👀", "✅", "❤️", "😄"];
    const HERE_TOKEN = /@here\b/gi;

    // URLs | target names (TOI-1234 / TIC 12345678) | @mentions. Built at runtime
    // so an environment lacking lookbehind falls back instead of a parse error.
    let TOKEN_SRC;
    try {
        new RegExp("(?<!x)");
        TOKEN_SRC = "(https?://[^\\s]+)|(\\bTOI[-\\s]?\\d+(?:\\.\\d+)?\\b|\\bTIC[-\\s]?\\d+\\b)|(?<![\\w@])@([A-Za-z0-9._-]+)";
    } catch (e) {
        TOKEN_SRC = "(https?://[^\\s]+)|(\\bTOI[-\\s]?\\d+(?:\\.\\d+)?\\b|\\bTIC[-\\s]?\\d+\\b)|@([A-Za-z0-9._-]+)";
    }

    let isMinimized = false;
    let unread = 0;
    let knownUsers = [];
    let acItems = [];
    let acIndex = -1;
    let typingSent = false;
    let typingStopTimer = null;
    const typingUsers = new Map();     // user -> clear-timeout id
    const els = new Map();             // message id -> { el, data }
    const origTitle = document.title;
    let flashTimer = null;

    // ----- identity --------------------------------------------------------
    let currentUser = (chatWindow.dataset.currentUser || "").trim();
    const getCurrentUser = () => currentUser || "Anonymous";
    const isMine = (data) =>
        !!(currentUser && data.user && data.kind !== "system" &&
           data.user.toLowerCase() === currentUser.toLowerCase());

    const onUserResolved = (u) => {
        if (!u || u === currentUser) return;
        currentUser = u;
        refreshOwnership();
    };
    if (window.MuscatWhoami && typeof window.MuscatWhoami.then === "function") {
        window.MuscatWhoami.then(onUserResolved);
    } else {
        fetch("/whoami", { headers: { "X-Requested-With": "fetch" } })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => onUserResolved(d && typeof d.user === "string" ? d.user.trim() : ""))
            .catch(() => {});
    }

    fetch("/chat/users", { headers: { "X-Requested-With": "fetch" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (d && Array.isArray(d.users)) knownUsers = d.users; })
        .catch(() => {});

    // ----- helpers ---------------------------------------------------------
    const expandHere = (text) => text.replace(HERE_TOKEN, window.location.href);

    const shortenUrl = (href) => {
        let label = href;
        try {
            const u = new URL(href);
            label = (u.origin === window.location.origin)
                ? (u.pathname + u.search + u.hash) || "/"
                : u.hostname + u.pathname;
        } catch (e) { /* raw href */ }
        return label.length > 64 ? label.slice(0, 61) + "…" : label;
    };

    const formatTime = (ts) => {
        const d = ts ? new Date(ts * 1000) : new Date();
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    };

    const makeLink = (href, textContent, title) => {
        const a = document.createElement("a");
        a.href = href;
        a.textContent = textContent;
        if (title) a.title = title;
        a.className = "chat-link";
        if (/^https?:/i.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
        return a;
    };

    // Render text as safe DOM nodes, linkifying URLs + target names and
    // highlighting @mentions (self-mentions stand out).
    const renderText = (container, text) => {
        const re = new RegExp(TOKEN_SRC, "gi");
        let last = 0, m;
        while ((m = re.exec(text)) !== null) {
            if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
            if (m[1]) {
                container.appendChild(makeLink(m[1], shortenUrl(m[1]), m[1]));
            } else if (m[2]) {
                container.appendChild(makeLink("/target?name=" + encodeURIComponent(m[2]), m[2], "Open " + m[2]));
            } else if (m[3] !== undefined) {
                const span = document.createElement("span");
                span.className = "chat-mention";
                if (currentUser && m[3].toLowerCase() === currentUser.toLowerCase()) {
                    span.classList.add("mention-self");
                }
                span.textContent = "@" + m[3];
                container.appendChild(span);
            }
            last = re.lastIndex;
            if (m.index === re.lastIndex) re.lastIndex++;
        }
        if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
    };

    // ----- unread / minimize ----------------------------------------------
    const clearUnread = () => { unread = 0; unreadBadge.textContent = "0"; unreadBadge.classList.add("hidden"); };
    const bumpUnread = () => {
        if (!isMinimized) return;
        unread += 1;
        unreadBadge.textContent = unread > 99 ? "99+" : String(unread);
        unreadBadge.classList.remove("hidden");
    };
    const setMinimized = (value) => {
        isMinimized = value;
        chatWindow.classList.toggle("minimized", value);
        minimizeChat.textContent = value ? "+" : "–";
        minimizeChat.setAttribute("aria-label", value ? "Expand chat" : "Minimize chat");
        if (!value) { clearUnread(); stopTitleFlash(); chatMessages.scrollTop = chatMessages.scrollHeight; }
        try { localStorage.setItem("muscat-chat-min", value ? "1" : "0"); } catch (e) {}
    };
    chatHeader.addEventListener("click", (e) => {
        if (e.target.closest("#chat-online")) return;  // let the online list be inspected
        setMinimized(!isMinimized);
    });

    // ----- title flash on mention -----------------------------------------
    const stopTitleFlash = () => { if (flashTimer) { clearInterval(flashTimer); flashTimer = null; } document.title = origTitle; };
    const startTitleFlash = () => {
        if (flashTimer || (document.hasFocus() && !isMinimized)) return;
        let on = false;
        flashTimer = setInterval(() => { document.title = on ? origTitle : "💬 New mention"; on = !on; }, 1000);
    };
    window.addEventListener("focus", stopTitleFlash);

    // ----- message rendering ----------------------------------------------
    const scrollToBottom = () => { chatMessages.scrollTop = chatMessages.scrollHeight; };

    const buildReactions = (el, data) => {
        let bar = el.querySelector(".reactions");
        if (bar) bar.remove();
        bar = document.createElement("div");
        bar.className = "reactions";
        (data.reactions || []).forEach((r) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "reaction-chip";
            if (currentUser && (r.users || []).some((u) => u.toLowerCase() === currentUser.toLowerCase())) {
                chip.classList.add("mine");
            }
            chip.title = (r.users || []).join(", ");
            chip.textContent = r.emoji + " " + r.count;
            chip.addEventListener("click", (e) => { e.stopPropagation(); socket.emit("toggle_reaction", { id: data.id, emoji: r.emoji }); });
            bar.appendChild(chip);
        });
        el.appendChild(bar);
    };

    const openEmojiPicker = (anchor, msgId) => {
        const existing = document.querySelector(".emoji-picker");
        if (existing) existing.remove();
        const picker = document.createElement("div");
        picker.className = "emoji-picker";
        PRESET_EMOJI.forEach((emoji) => {
            const b = document.createElement("button");
            b.type = "button";
            b.textContent = emoji;
            b.addEventListener("click", (e) => {
                e.stopPropagation();
                socket.emit("toggle_reaction", { id: msgId, emoji });
                picker.remove();
            });
            picker.appendChild(b);
        });
        anchor.appendChild(picker);
        setTimeout(() => {
            const close = (ev) => { if (!picker.contains(ev.target)) { picker.remove(); document.removeEventListener("click", close); } };
            document.addEventListener("click", close);
        }, 0);
    };

    const beginEdit = (entry) => {
        const { el, data } = entry;
        const body = el.querySelector(".message-body");
        const ta = document.createElement("textarea");
        ta.className = "edit-input";
        ta.value = data.text;
        ta.rows = 2;
        body.replaceWith(ta);
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
        const finish = (commit) => {
            const newText = expandHere(ta.value).trim();
            const restored = document.createElement("div");
            restored.className = "message-body";
            renderText(restored, data.text);
            ta.replaceWith(restored);
            if (commit && newText && newText !== data.text) {
                socket.emit("edit_message", { id: data.id, text: newText });
            }
        };
        ta.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false); }
        });
        ta.addEventListener("blur", () => finish(false));
    };

    const renderMessage = (data) => {
        if (chatEmpty && chatEmpty.parentNode) chatEmpty.remove();

        // System messages get a compact, centered treatment.
        if (data.kind === "system") {
            const sys = document.createElement("div");
            sys.className = "message system";
            const body = document.createElement("div");
            body.className = "message-body";
            renderText(body, data.text);
            const time = document.createElement("span");
            time.className = "time";
            time.textContent = formatTime(data.ts);
            body.appendChild(time);
            sys.appendChild(body);
            chatMessages.appendChild(sys);
            if (data.id != null) els.set(data.id, { el: sys, data });
            scrollToBottom();
            return;
        }

        const el = document.createElement("div");
        el.className = "message " + (isMine(data) ? "me" : "them");
        if (data.ephemeral) el.classList.add("ephemeral");
        if (data.mentions && currentUser &&
            data.mentions.some((n) => n.toLowerCase() === currentUser.toLowerCase())) {
            el.classList.add("mention-me");
        }
        if (data.id != null) el.dataset.id = data.id;

        const meta = document.createElement("div");
        meta.className = "message-meta";
        const user = document.createElement("span");
        user.className = "user";
        user.textContent = isMine(data) ? "You" : (data.user || "Anonymous");
        const time = document.createElement("span");
        time.className = "time";
        time.textContent = formatTime(data.ts);
        meta.append(user, time);
        if (data.ephemeral) {
            const tag = document.createElement("span");
            tag.className = "tag"; tag.textContent = "test";
            meta.appendChild(tag);
        }
        const editedTag = document.createElement("span");
        editedTag.className = "edited";
        editedTag.textContent = "(edited)";
        if (!data.edited) editedTag.style.display = "none";
        meta.appendChild(editedTag);

        const row = document.createElement("div");
        row.className = "message-row";
        const body = document.createElement("div");
        body.className = "message-body";
        renderText(body, data.text);
        row.appendChild(body);

        // Action buttons (edit/delete gated to own messages via CSS + .me class).
        if (!data.ephemeral && data.id != null) {
            const actions = document.createElement("div");
            actions.className = "message-actions";
            const reactBtn = document.createElement("button");
            reactBtn.type = "button"; reactBtn.className = "act react"; reactBtn.title = "React"; reactBtn.textContent = "☺";
            reactBtn.addEventListener("click", (e) => { e.stopPropagation(); openEmojiPicker(actions, data.id); });
            const editBtn = document.createElement("button");
            editBtn.type = "button"; editBtn.className = "act edit"; editBtn.title = "Edit"; editBtn.textContent = "✎";
            editBtn.addEventListener("click", (e) => { e.stopPropagation(); beginEdit(els.get(data.id)); });
            const delBtn = document.createElement("button");
            delBtn.type = "button"; delBtn.className = "act delete"; delBtn.title = "Delete"; delBtn.textContent = "🗑";
            delBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                const doDelete = () => socket.emit("delete_message", { id: data.id });
                if (window.showConfirmModal) {
                    window.showConfirmModal("Delete message", "Delete this message? This cannot be undone.",
                        { confirmLabel: "Delete", confirmClass: "btn primary" }).then((ok) => { if (ok) doDelete(); });
                } else {
                    doDelete();
                }
            });
            actions.append(reactBtn, editBtn, delBtn);
            row.appendChild(actions);
        }
        el.append(meta, row);
        chatMessages.appendChild(el);
        if (data.id != null) els.set(data.id, { el, data });
        if (!data.ephemeral && data.id != null) buildReactions(el, data);
        scrollToBottom();
    };

    // Re-apply own/other styling once identity resolves (history may render first).
    const refreshOwnership = () => {
        els.forEach((entry) => {
            const { el, data } = entry;
            if (data.kind === "system") return;
            el.classList.toggle("me", isMine(data));
            el.classList.toggle("them", !isMine(data));
            const userSpan = el.querySelector(".message-meta .user");
            if (userSpan) userSpan.textContent = isMine(data) ? "You" : (data.user || "Anonymous");
        });
    };

    // ----- sending ---------------------------------------------------------
    const send = () => {
        const text = expandHere(messageInput.value).trim();
        if (!text) return;
        socket.emit("message", { user: getCurrentUser(), text });
        messageInput.value = "";
        stopTyping();
        hideAutocomplete();
        messageInput.focus();
    };
    sendButton.addEventListener("click", send);

    messageInput.addEventListener("keydown", (e) => {
        if (!autocompleteEl.classList.contains("hidden")) {
            if (e.key === "ArrowDown") { e.preventDefault(); moveAc(1); return; }
            if (e.key === "ArrowUp") { e.preventDefault(); moveAc(-1); return; }
            if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); acceptAc(); return; }
            if (e.key === "Escape") { e.preventDefault(); hideAutocomplete(); return; }
        }
        if (e.key === "Enter") { e.preventDefault(); send(); }
    });

    messageInput.addEventListener("input", () => {
        // Live-expand "@here " so the sender previews the shared URL.
        const v = messageInput.value;
        if (/@here(?=\s)/i.test(v)) {
            const expanded = v.replace(/@here(?=\s)/gi, window.location.href);
            messageInput.value = expanded;
            messageInput.setSelectionRange(expanded.length, expanded.length);
        }
        sendTyping();
        updateAutocomplete();
    });

    // ----- @mention autocomplete ------------------------------------------
    const mentionToken = () => {
        const upto = messageInput.value.slice(0, messageInput.selectionStart);
        const m = upto.match(/@([A-Za-z0-9._-]*)$/);
        return m ? m[1] : null;
    };
    const hideAutocomplete = () => { autocompleteEl.classList.add("hidden"); autocompleteEl.innerHTML = ""; acItems = []; acIndex = -1; };
    const renderAutocomplete = () => {
        autocompleteEl.innerHTML = "";
        acItems.forEach((name, i) => {
            const item = document.createElement("div");
            item.className = "ac-item" + (i === acIndex ? " active" : "");
            item.textContent = name;
            item.addEventListener("mousedown", (e) => { e.preventDefault(); acIndex = i; acceptAc(); });
            autocompleteEl.appendChild(item);
        });
        autocompleteEl.classList.toggle("hidden", acItems.length === 0);
    };
    const updateAutocomplete = () => {
        const token = mentionToken();
        if (token === null) { hideAutocomplete(); return; }
        const low = token.toLowerCase();
        acItems = knownUsers.filter((u) => u.toLowerCase().startsWith(low)).slice(0, 6);
        acIndex = acItems.length ? 0 : -1;
        renderAutocomplete();
    };
    const moveAc = (delta) => {
        if (!acItems.length) return;
        acIndex = (acIndex + delta + acItems.length) % acItems.length;
        renderAutocomplete();
    };
    const acceptAc = () => {
        if (acIndex < 0 || acIndex >= acItems.length) { hideAutocomplete(); return; }
        const name = acItems[acIndex];
        const start = messageInput.selectionStart;
        const before = messageInput.value.slice(0, start).replace(/@([A-Za-z0-9._-]*)$/, "@" + name + " ");
        const after = messageInput.value.slice(start);
        messageInput.value = before + after;
        const caret = before.length;
        messageInput.setSelectionRange(caret, caret);
        hideAutocomplete();
    };

    // ----- typing indicator ------------------------------------------------
    const sendTyping = () => {
        if (!typingSent) { socket.emit("typing", { typing: true }); typingSent = true; }
        clearTimeout(typingStopTimer);
        typingStopTimer = setTimeout(stopTyping, 2500);
    };
    function stopTyping() {
        clearTimeout(typingStopTimer);
        if (typingSent) { socket.emit("typing", { typing: false }); typingSent = false; }
    }
    const renderTyping = () => {
        const names = Array.from(typingUsers.keys());
        if (!names.length) { typingEl.textContent = ""; return; }
        typingEl.textContent = names.length === 1
            ? names[0] + " is typing…"
            : names.slice(0, 2).join(", ") + (names.length > 2 ? " and others" : "") + " are typing…";
    };

    // ----- socket events ---------------------------------------------------
    socket.on("history", (d) => {
        (d && Array.isArray(d.messages) ? d.messages : []).forEach(renderMessage);
        scrollToBottom();
    });
    socket.on("message", (data) => {
        if (!data || typeof data.text !== "string") return;
        renderMessage(data);
        bumpUnread();
    });
    socket.on("message_edited", (data) => {
        const entry = els.get(data.id);
        if (!entry) return;
        entry.data = { ...entry.data, text: data.text, edited: true };
        const body = entry.el.querySelector(".message-body");
        if (body) { body.innerHTML = ""; renderText(body, data.text); }
        const edited = entry.el.querySelector(".edited");
        if (edited) edited.style.display = "";
    });
    socket.on("message_deleted", (data) => {
        const entry = els.get(data.id);
        if (entry) { entry.el.remove(); els.delete(data.id); }
    });
    socket.on("reaction_updated", (data) => {
        const entry = els.get(data.id);
        if (!entry) return;
        entry.data = { ...entry.data, reactions: data.reactions };
        buildReactions(entry.el, entry.data);
    });
    socket.on("mention", () => { bumpUnread(); startTitleFlash(); });
    socket.on("chat_error", (d) => { if (d && d.error) console.warn("chat:", d.error); });

    socket.on("typing", (d) => {
        if (!d || !d.user) return;
        if (currentUser && d.user.toLowerCase() === currentUser.toLowerCase()) return;
        if (d.typing) {
            clearTimeout(typingUsers.get(d.user));
            typingUsers.set(d.user, setTimeout(() => { typingUsers.delete(d.user); renderTyping(); }, 4000));
        } else {
            clearTimeout(typingUsers.get(d.user));
            typingUsers.delete(d.user);
        }
        renderTyping();
    });

    const setConnected = (connected) => {
        statusDot.classList.toggle("online", connected);
        statusDot.title = connected ? "Connected" : "Disconnected";
        if (!connected) onlineEl.textContent = "offline";
    };
    socket.on("connect", () => setConnected(true));
    socket.on("disconnect", () => setConnected(false));
    socket.on("presence", (d) => {
        const users = (d && Array.isArray(d.users)) ? d.users : [];
        const n = (d && typeof d.count === "number") ? d.count : users.length;
        onlineEl.textContent = n + " online";
        onlineEl.title = users.length ? "Online: " + users.join(", ") : "";
    });

    // Restore prior minimize state (UI only).
    let storedMin = "0";
    try { storedMin = localStorage.getItem("muscat-chat-min") || "0"; } catch (e) {}
    setMinimized(storedMin === "1");
});
