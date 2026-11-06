'use strict';

const items = document.getElementById('items');

window.requirementsApi.get().then(({ verdict }) => {
  const entries = [
    ...verdict.blocking.map((item) => ({ ...item, kind: 'blocking' })),
    ...verdict.warnings.map((item) => ({ ...item, kind: 'warning' })),
  ];
  for (const entry of entries) {
    const li = document.createElement('li');
    li.className = entry.kind;
    const title = document.createElement('b');
    title.textContent = entry.title;
    const detail = document.createElement('span');
    detail.textContent = entry.detail;
    li.append(title, detail);
    items.append(li);
  }
});

document.getElementById('copy').addEventListener('click', async () => {
  await window.requirementsApi.copy();
  document.getElementById('copied').textContent = 'Copied to clipboard';
});
document.getElementById('quit').addEventListener('click', () => window.requirementsApi.quit());
