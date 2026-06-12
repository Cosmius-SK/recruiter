// Mobile nav toggle
const toggle = document.querySelector(".nav-toggle");
const links = document.querySelector(".nav-links");
if (toggle && links) {
  toggle.addEventListener("click", () => links.classList.toggle("open"));
}

// Mark the active nav link
const here = location.pathname.split("/").pop() || "index.html";
document.querySelectorAll(".nav-links a").forEach((a) => {
  if (a.getAttribute("href") === here) a.classList.add("active");
});

// Scroll-reveal
const observer = new IntersectionObserver(
  (entries) => entries.forEach((e) => e.isIntersecting && e.target.classList.add("in")),
  { threshold: 0.12 }
);
document.querySelectorAll(".reveal").forEach((el) => observer.observe(el));
