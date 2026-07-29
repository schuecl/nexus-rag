(() => {
  const toggle = document.querySelector(".nav-toggle");
  const navigation = document.getElementById("primary-navigation");

  if (!toggle || !navigation) {
    return;
  }

  const setOpen = (open) => {
    navigation.classList.toggle("open", open);
    toggle.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  };

  toggle.addEventListener("click", () => {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });

  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) {
      setOpen(false);
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 820) {
      setOpen(false);
    }
  });
})();
