(() => {
  const add = document.querySelector('[data-add-career]');
  if (add) add.addEventListener('click', () => {
    document.querySelector('[data-career-rows]').append(document.querySelector('#ingreso-career-row').content.cloneNode(true));
  });
  const form = document.querySelector('[data-ingreso-selection]');
  if (form) {
    const rows = [...form.querySelectorAll('input[name="task_id"]:not(:disabled)')];
    const page = form.querySelector('[data-select-page]');
    const all = form.querySelector('[data-select-filter]');
    const button = form.querySelector('[data-execute-button]');
    let anchor = null;
    const update = () => {
      const count = rows.filter(row => row.checked).length;
      page.disabled = !rows.length || all.checked;
      page.checked = rows.length > 0 && count === rows.length;
      page.indeterminate = count > 0 && count < rows.length;
      rows.forEach(row => row.disabled = all.checked);
      form.querySelector('[data-selection-count]').textContent = all.checked ? 'Todo el filtro seleccionado' : `${count} seleccionados en esta pagina`;
      if (button) button.disabled = !(count || all.checked);
    };
    rows.forEach((row,index) => row.addEventListener('click', event => {
      if (anchor !== null && (event.shiftKey || event.ctrlKey)) rows.slice(Math.min(anchor,index),Math.max(anchor,index)+1).forEach(item => item.checked = row.checked);
      anchor = index;
      update();
    }));
    page.addEventListener('change', () => { rows.forEach(row => row.checked = page.checked); update(); });
    all.addEventListener('change', update);
    update();
  }
  const progress = document.querySelector('[data-ingreso-progress]');
  if (progress) {
    const poll = async () => {
      try {
        const response = await fetch(progress.dataset.ingresoProgress, {cache:'no-store', headers:{Accept:'application/json'}});
        if (!response.ok || response.redirected) throw new Error('No disponible');
        const data = await response.json();
        progress.querySelector('[data-job-message]').textContent = data.message;
        const bar = progress.querySelector('progress');
        if (bar) { bar.max = data.total || 1; bar.value = data.processed; }
        if (!['queued','queued_execute','running'].includes(data.status)) { location.reload(); return; }
      } catch (error) {
        progress.querySelector('[data-job-message]').textContent = 'Conexion interrumpida. Reintentando consulta de progreso...';
      }
      setTimeout(poll,2500);
    };
    setTimeout(poll,1500);
  }
})();
