(function () {
    function resolveTarget(trigger) {
        const selector = trigger.getAttribute("data-bs-target") || trigger.getAttribute("href");
        if (!selector || selector === "#") {
            return null;
        }
        try {
            return document.querySelector(selector);
        } catch (error) {
            return null;
        }
    }

    function closeDropdown(dropdown) {
        if (!dropdown) {
            return;
        }
        const menu = dropdown.querySelector(".dropdown-menu");
        const trigger = dropdown.querySelector("[data-bs-toggle=\"dropdown\"]");
        dropdown.classList.remove("show");
        if (menu) {
            menu.classList.remove("show");
        }
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
        }
    }

    function closeAllDropdowns(exceptDropdown) {
        document.querySelectorAll(".dropdown.show").forEach(function (dropdown) {
            if (dropdown !== exceptDropdown) {
                closeDropdown(dropdown);
            }
        });
    }

    let offcanvasBackdrop = null;

    function ensureBackdrop() {
        if (!offcanvasBackdrop) {
            offcanvasBackdrop = document.createElement("div");
            offcanvasBackdrop.className = "offcanvas-backdrop";
            offcanvasBackdrop.addEventListener("click", function () {
                closeOpenOffcanvas();
            });
        }
        return offcanvasBackdrop;
    }

    function openOffcanvas(panel) {
        if (!panel) {
            return;
        }
        closeOpenOffcanvas();
        panel.classList.add("show");
        panel.setAttribute("aria-modal", "true");
        document.body.classList.add("ui-offcanvas-open");
        if (!ensureBackdrop().isConnected) {
            document.body.appendChild(offcanvasBackdrop);
        }
    }

    function closeOffcanvas(panel) {
        if (!panel) {
            return;
        }
        panel.classList.remove("show");
        panel.removeAttribute("aria-modal");
        if (!document.querySelector(".offcanvas.show")) {
            document.body.classList.remove("ui-offcanvas-open");
            if (offcanvasBackdrop && offcanvasBackdrop.isConnected) {
                offcanvasBackdrop.remove();
            }
        }
    }

    function closeOpenOffcanvas() {
        document.querySelectorAll(".offcanvas.show").forEach(function (panel) {
            closeOffcanvas(panel);
        });
    }

    function toggleCollapse(target, trigger) {
        if (!target) {
            return;
        }
        const isOpen = target.classList.toggle("show");
        const targetId = target.id ? "#" + target.id : null;
        document.querySelectorAll("[data-bs-toggle=\"collapse\"]").forEach(function (item) {
            const selector = item.getAttribute("data-bs-target") || item.getAttribute("href");
            if (selector && selector === targetId) {
                item.setAttribute("aria-expanded", String(isOpen));
            }
        });
        if (trigger) {
            trigger.setAttribute("aria-expanded", String(isOpen));
        }
    }

    function hideAlert(alertEl) {
        if (!alertEl) {
            return;
        }
        alertEl.classList.remove("show");
        window.setTimeout(function () {
            alertEl.remove();
        }, 180);
    }

    function LocalToast(element, options) {
        this.element = element;
        this.delay = Number(options && options.delay) || 5000;
        this.timer = null;
        element._localToast = this;
    }

    LocalToast.prototype.show = function () {
        const element = this.element;
        window.requestAnimationFrame(function () {
            element.classList.add("show");
        });
        if (this.delay > 0) {
            clearTimeout(this.timer);
            this.timer = window.setTimeout(this.hide.bind(this), this.delay);
        }
    };

    LocalToast.prototype.hide = function () {
        clearTimeout(this.timer);
        this.element.classList.remove("show");
        window.setTimeout(function (element) {
            element.dispatchEvent(new CustomEvent("hidden.bs.toast"));
            if (element.isConnected) {
                element.remove();
            }
        }, 180, this.element);
    };

    window.bootstrap = window.bootstrap || {};
    window.bootstrap.Toast = LocalToast;

    document.addEventListener("click", function (event) {
        const dismissTrigger = event.target.closest("[data-bs-dismiss]");
        if (dismissTrigger) {
            const action = dismissTrigger.getAttribute("data-bs-dismiss");
            if (action === "alert") {
                hideAlert(dismissTrigger.closest(".alert"));
                return;
            }
            if (action === "toast") {
                const toastEl = dismissTrigger.closest(".toast");
                if (toastEl && toastEl._localToast) {
                    toastEl._localToast.hide();
                } else if (toastEl) {
                    hideAlert(toastEl);
                }
                return;
            }
            if (action === "offcanvas") {
                closeOffcanvas(dismissTrigger.closest(".offcanvas"));
                return;
            }
        }

        const dropdownTrigger = event.target.closest("[data-bs-toggle=\"dropdown\"]");
        if (dropdownTrigger) {
            event.preventDefault();
            const dropdown = dropdownTrigger.closest(".dropdown");
            const menu = dropdown ? dropdown.querySelector(".dropdown-menu") : null;
            const isOpen = Boolean(menu && menu.classList.contains("show"));
            closeAllDropdowns(dropdown);
            if (dropdown && menu && !isOpen) {
                dropdown.classList.add("show");
                menu.classList.add("show");
                dropdownTrigger.setAttribute("aria-expanded", "true");
            } else if (dropdown) {
                closeDropdown(dropdown);
            }
            return;
        }

        const offcanvasTrigger = event.target.closest("[data-bs-toggle=\"offcanvas\"]");
        if (offcanvasTrigger) {
            event.preventDefault();
            openOffcanvas(resolveTarget(offcanvasTrigger));
            return;
        }

        const collapseTrigger = event.target.closest("[data-bs-toggle=\"collapse\"]");
        if (collapseTrigger) {
            event.preventDefault();
            toggleCollapse(resolveTarget(collapseTrigger), collapseTrigger);
            return;
        }

        if (!event.target.closest(".dropdown")) {
            closeAllDropdowns(null);
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeAllDropdowns(null);
            closeOpenOffcanvas();
        }
    });
})();
