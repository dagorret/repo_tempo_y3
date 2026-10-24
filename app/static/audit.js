(() => {
  const form = document.querySelector('[data-selection-form]');
  if (form) {
    const rows = [...form.querySelectorAll('input[name="dni"]')];
    const page = form.querySelector('[data-select-page]');
    const all = form.querySelector('[data-select-filter]');
    const button = form.querySelector('[data-preview-button]');
    let anchor = null;
    const update = () => {
      const selected = rows.filter(row => row.checked).length;
      page.disabled = !rows.length || all.checked;
      page.checked = !!rows.length && selected === rows.length;
      page.indeterminate = selected > 0 && selected < rows.length;
      button.disabled = !(selected || all.checked);
      rows.forEach(row => row.disabled = all.checked);
    };
    page.addEventListener('change', () => { rows.forEach(row => row.checked = page.checked); update(); });
    all.addEventListener('change', update);
    rows.forEach((row, index) => row.addEventListener('click', event => {
      if (anchor !== null && (event.shiftKey || event.ctrlKey)) {
        rows.slice(Math.min(anchor,index), Math.max(anchor,index)+1).forEach(item => item.checked = row.checked);
      }
      anchor = index;
      update();
    }));
    update();
  }
  const job = document.querySelector('[data-progress-url]');
  if (!job) return;
  const poll = async () => {
    try {
      const response = await fetch(job.dataset.progressUrl, {headers:{Accept:'application/json'}, cache:'no-store'});
      if (!response.ok || response.redirected) throw new Error('Conexion interrumpida o sesion vencida.');
      const data = await response.json();
      job.querySelector('[data-job-message]').textContent = data.message;
      const bar = job.querySelector('progress');
      if (bar) bar.value = data.progress;
      if (!['queued','running'].includes(data.status)) { location.reload(); return; }
    } catch (error) {
      job.querySelector('[data-job-message]').textContent = 'No se pudo actualizar el progreso. Reintentando la consulta...';
    }
    setTimeout(poll, 2500);
  };
  setTimeout(poll, 1000);
})();
