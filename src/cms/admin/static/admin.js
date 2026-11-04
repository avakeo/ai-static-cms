// 確認ダイアログ。form に data-confirm="文言" を付けると送信前に確認する。
// インラインの onsubmit に書くと、タイトルに ' が入ったときに JS が壊れるため。
document.addEventListener("submit", (e) => {
  const message = e.target.dataset.confirm;
  if (message && !window.confirm(message)) e.preventDefault();
});
