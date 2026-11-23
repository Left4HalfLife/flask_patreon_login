"use strict";

const releases = document.getElementById("releases");
const addRelease = document.getElementById("add-release");

function renumberReleases() {
  releases.querySelectorAll(".release-row").forEach((row, index) => {
    row.querySelectorAll("[name]").forEach((input) => {
      input.name = input.name.replace(/release_(tiers|public)_.+$/, `release_$1_${index}`);
    });
  });
}

if (addRelease) {
  addRelease.addEventListener("click", () => {
    if (releases.children.length >= 20) {
      window.alert("Use at most 20 timed grants.");
      return;
    }
    const template = document.getElementById("release-template");
    releases.appendChild(template.content.cloneNode(true));
    renumberReleases();
  });
  releases.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const row = button.closest(".release-row");
    if (button.hasAttribute("data-remove-release")) {
      row.remove();
      renumberReleases();
    } else if (button.dataset.days) {
      const input = row.querySelector('[name="release_at"]');
      const date = input.value ? new Date(`${input.value}Z`) : new Date();
      date.setUTCDate(date.getUTCDate() + Number(button.dataset.days));
      input.value = date.toISOString().slice(0, 16);
    }
  });
}

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
