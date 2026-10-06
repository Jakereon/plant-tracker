// Before/after compare slider
document.addEventListener('input', (e) => {
  if (e.target.matches('.cmp-range')) {
    e.target.closest('.compare').style.setProperty('--pos', e.target.value + '%');
  }
});
document.addEventListener('change', (e) => {
  if (e.target.matches('.cmp-sel')) {
    document.getElementById(e.target.dataset.target).src = e.target.value;
  }
});
// Identify: show a spinner while the photo uploads and is analysed
document.addEventListener('submit', (e) => {
  if (e.target.id === 'idform') {
    document.getElementById('idwait')?.classList.remove('hidden');
    e.target.querySelector('.dropzone')?.classList.add('hidden');
  }
});
