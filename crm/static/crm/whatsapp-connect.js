(() => {
  const status = document.getElementById('connect-status');
  const csrf = document.querySelector('#csrf-form input[name=csrfmiddlewaretoken]')?.value;
  async function post(url, data) {
    const res = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(data || {})});
    if (res.redirected) throw new Error('Sua sessão expirou. Entre novamente no Rastro.');
    let body;
    try { body = await res.json(); } catch (_) { throw new Error('Não foi possível concluir. Atualize a página e tente novamente.'); }
    if (!res.ok) throw new Error(body.message || 'Não foi possível concluir a conexão.');
    return body;
  }
  const qrButton = document.getElementById('qr-start');
  if (qrButton) {
    const image = document.getElementById('qr-image');
    let busy = false, timer;
    async function refresh() {
      if (busy || document.hidden) return;
      busy = true;
      qrButton.disabled = true;
      try {
        const result = await post(qrButton.dataset.url);
        if (result.connected) { window.location.reload(); return; }
        if (result.qrcode) {
          image.src = result.qrcode;
          image.hidden = false;
          status.textContent = 'Aguardando a leitura pelo celular. O código atualiza automaticamente.';
        } else { status.textContent = 'Preparando o código. Aguarde alguns instantes…'; }
        timer = setTimeout(refresh, 8000);
      } catch (error) {
        status.textContent = error.message;
        qrButton.textContent = 'Tentar novamente';
        qrButton.disabled = false;
      } finally { busy = false; }
    }
    qrButton.addEventListener('click', () => { clearTimeout(timer); refresh(); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && qrButton.disabled && !busy) refresh(); });
    window.addEventListener('pagehide', () => clearTimeout(timer));
  }
  const button = document.getElementById('coexistence-start');
  if (button) button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Conclua a autorização na janela da Meta.';
    try {
      const config = JSON.parse(document.getElementById('whatsapp-config').textContent);
      if (!window.Omnizapi) throw new Error('Não foi possível carregar a conexão. Atualize a página.');
      const client = window.Omnizapi.newClient({publicKey: config.publicKey});
      const result = await client.connect({channelId: config.channelId});
      if (!result.success) throw new Error('Conexão não concluída. Você pode tentar novamente.');
      if (result.coexistence !== true) throw new Error('Escolha Coexistência para manter o aplicativo WhatsApp Business. Nenhuma troca de número foi aplicada pelo Rastro.');
      await post(config.finishUrl, result);
      window.location.reload();
    } catch (error) {
      status.textContent = error.code === 'POPUP_BLOCKED' ? 'Permita a janela de conexão no navegador e tente novamente.' : (error.message || 'A conexão não foi concluída. Tente novamente.');
      button.disabled = false;
    }
  });
})();
