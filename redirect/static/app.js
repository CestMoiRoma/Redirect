// Redirect front end: one page over the JSON API. No build step, no dependency.
// Every user-facing text comes from strings.json; validation happens on the server only.
'use strict'
;(() => {
  const COPIED_MS = 2000
  const LOG_PAGE = 25
  // Fields whose errors the API reports by name (a form has the ones it shows)
  const FIELDS = ['slug', 'name', 'target', 'design']

  const state = {
    strings: {},
    baseUrl: '',
    redirects: [],
    filter: '',
    highlightId: null, // the entry just created or saved
    editing: null,
    deleting: null,
    loginRequired: false,
    keyExists: false,
    settings: null, // as last saved on the server
    logsBefore: null, // the `before` of the next page of the log
    qrcodes: [],
    qrEditing: null, // the QR code the editor changes; null while it creates one
    qrLogo: null, // the logo of the editor, as a data URI
    qrVersion: 0, // changes the images' URLs after each reload, so an edited QR code is fetched again
    appShown: false,
  }

  const $ = (selector, root = document) => root.querySelector(selector)

  // --- Texts ------------------------------------------------------------------------------------------

  function t(key, values = {}) {
    const text = state.strings[key]
    if (text === undefined) throw new Error(`Missing string: ${key}`)
    return text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match))
  }

  function applyStrings(root) {
    root.querySelectorAll('[data-i18n]').forEach((el) => {
      el.textContent = t(el.dataset.i18n)
    })
    root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      el.placeholder = t(el.dataset.i18nPlaceholder)
    })
    root.querySelectorAll('[data-i18n-aria-label]').forEach((el) => {
      el.setAttribute('aria-label', t(el.dataset.i18nAriaLabel))
    })
    root.querySelectorAll('[data-i18n-alt]').forEach((el) => {
      el.alt = t(el.dataset.i18nAlt)
    })
  }

  // --- API --------------------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, message, errors) {
      super(message)
      this.status = status
      this.errors = errors || {}
    }
  }

  async function api(method, path, body) {
    let response
    try {
      response = await fetch(path, {
        method,
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      throw new ApiError(0, t('status.network'))
    }
    // The session is gone (expired, logged out elsewhere, new password): back to the login screen
    if (response.status === 401 && path !== '/api/login') showLogin()
    if (response.status === 204) return null
    let data = null
    try {
      data = await response.json()
    } catch {
      data = null
    }
    if (!response.ok) {
      throw new ApiError(response.status, (data && data.detail) || t('status.server_error'), data && data.errors)
    }
    return data
  }

  async function reload() {
    const data = await api('GET', '/api/redirects')
    state.baseUrl = data.base_url
    state.redirects = data.redirects
    render()
    renderNanos()
    state.qrcodes = (await api('GET', '/api/qrcodes')).qrcodes
    state.qrVersion += 1
    renderQrs()
  }

  function ofKind(kind) {
    return state.redirects.filter((r) => r.kind === kind)
  }

  // --- Status line ------------------------------------------------------------------------------------

  // Where each kind of entry is listed: its list, its heading and its status line
  const PLACES = {
    link: { list: '#list', title: '#list-title', status: '#status' },
    nano: { list: '#nano-list', title: '#nano-title', status: '#nano-status' },
    qr: { list: '#qr-list', title: '#qr-title', status: '#qr-status' },
  }

  // The status line tells the outcome of the last action: a new action starts without it
  function clearStatus(place = PLACES.link) {
    const status = $(place.status)
    status.textContent = ''
    status.hidden = true
  }

  function setStatus(message, isError = false, place = PLACES.link) {
    const status = $(place.status)
    status.textContent = message
    status.classList.toggle('status-error', isError)
    status.hidden = false
  }

  // --- List -------------------------------------------------------------------------------------------

  function matches(redirect, query) {
    return [redirect.name, redirect.slug, redirect.target, redirect.public_url].some((value) =>
      value.toLowerCase().includes(query),
    )
  }

  // A URL as text nodes with a line-break opportunity after each "/" of its path (not inside "://"),
  // so a long URL wraps between segments rather than in the middle of a word
  function breakableUrl(url) {
    const parts = url.split(/(?<=[^/:]\/)/)
    return parts.flatMap((part, index) => (index ? [document.createElement('wbr'), part] : [part]))
  }

  // One entry of a list. A NanoURL shows its short link first, and no second link.
  function renderItem(redirect) {
    const item = $('#item-template').content.firstElementChild.cloneNode(true)
    const main = redirect.kind === 'nano' ? redirect.short_url : redirect.public_url
    item.dataset.id = redirect.id
    item.classList.toggle('is-new', redirect.id === state.highlightId)
    const link = $('.link', item)
    link.href = main
    link.replaceChildren(...breakableUrl(main))
    $('.item-short', item).hidden = redirect.kind === 'nano'
    $('.item-name', item).textContent = redirect.name
    $('.target-url', item).replaceChildren(...breakableUrl(redirect.target))
    const short = $('.short-link', item)
    short.href = redirect.short_url
    short.textContent = redirect.short_url
    $('[data-action="copy"]', item).setAttribute('aria-label', t('list.copy.aria', { name: redirect.name }))
    $('[data-action="edit"]', item).setAttribute('aria-label', t('list.edit.aria', { name: redirect.name }))
    $('[data-action="delete"]', item).setAttribute('aria-label', t('list.delete.aria', { name: redirect.name }))
    return item
  }

  function render() {
    const links = ofKind('link')
    const total = links.length
    const query = state.filter.trim().toLowerCase()
    const shown = query ? links.filter((r) => matches(r, query)) : links

    const list = $('#list')
    list.replaceChildren(...shown.map(renderItem))
    list.hidden = shown.length === 0

    const count = $('#count')
    count.textContent = total === 1 ? t('list.count.one') : t('list.count.other', { count: total })
    count.hidden = total === 0
    $('#empty').hidden = total !== 0
    $('#filter-box').hidden = total === 0

    const noMatch = $('#no-match')
    noMatch.hidden = total === 0 || shown.length > 0
    noMatch.textContent = noMatch.hidden ? '' : t('list.filter.none', { query: state.filter.trim() })

    document.querySelectorAll('[data-base-url]').forEach((el) => {
      el.textContent = `${state.baseUrl}/redirect/`
    })
  }

  function findRedirect(id) {
    return state.redirects.find((r) => r.id === id) || null
  }

  function focusInItem(id, action, place = PLACES.link) {
    const button = document.querySelector(`${place.list} [data-id="${id}"] [data-action="${action}"]`)
    if (button) button.focus()
  }

  // --- Copy -------------------------------------------------------------------------------------------

  async function copyText(text) {
    // The Clipboard API only exists in secure contexts; a LAN tool is usually plain http://
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text)
        return true
      } catch {
        // fall back below
      }
    }
    const helper = document.createElement('textarea')
    helper.value = text
    helper.setAttribute('readonly', '')
    helper.className = 'clipboard-helper'
    document.body.append(helper)
    helper.select()
    let copied = false
    try {
      copied = document.execCommand('copy')
    } catch {
      copied = false
    }
    helper.remove()
    return copied
  }

  const copyTimers = new WeakMap()

  async function copyLink(button, url, place = PLACES.link) {
    const copied = await copyText(url)
    button.focus()
    button.textContent = copied ? t('list.copied') : t('list.copy.failed')
    button.classList.toggle('is-copied', copied)
    button.classList.toggle('is-failed', !copied)
    // The status line is announced to screen readers: it must never keep a stale "copied" message
    setStatus(copied ? t('status.copied') : t('status.copy_failed'), !copied, place)
    clearTimeout(copyTimers.get(button))
    copyTimers.set(
      button,
      setTimeout(() => {
        button.textContent = t('list.copy')
        button.classList.remove('is-copied', 'is-failed')
      }, COPIED_MS),
    )
  }

  // --- Forms ------------------------------------------------------------------------------------------

  // The fields a form has: a NanoURL's form has no path
  function fieldsOf(form) {
    return FIELDS.filter((name) => form.elements[name])
  }

  function formValues(form) {
    return Object.fromEntries(fieldsOf(form).map((name) => [name, form.elements[name].value]))
  }

  function clearErrors(form) {
    for (const name of fieldsOf(form)) {
      const input = form.elements[name]
      input.removeAttribute('aria-invalid')
      const error = $(`#${input.id}-error`)
      error.textContent = ''
      error.hidden = true
    }
    const formError = $('.form-error', form)
    formError.textContent = ''
    formError.hidden = true
  }

  function showErrors(form, error) {
    let first = null
    for (const name of fieldsOf(form)) {
      const message = error.errors[name]
      if (!message) continue
      const input = form.elements[name]
      input.setAttribute('aria-invalid', 'true')
      const slot = $(`#${input.id}-error`)
      slot.textContent = message
      slot.hidden = false
      first = first || input
    }
    if (first) {
      first.focus()
    } else {
      const formError = $('.form-error', form)
      formError.textContent = error.message
      formError.hidden = false
    }
  }

  async function submitting(form, action, place = PLACES.link) {
    const submit = $('[type="submit"]', form)
    submit.disabled = true
    clearErrors(form)
    clearStatus(place)
    try {
      await action()
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      if (error.status === 401) return // the login screen has replaced the page
      if (error.status === 404) {
        // The entry was deleted elsewhere: close, refresh, tell, and keep the keyboard in the list
        form.closest('dialog')?.close()
        state.highlightId = null
        await reload().catch(() => {})
        setStatus(error.message, true, place)
        $(place.title).focus()
      } else {
        showErrors(form, error)
      }
    } finally {
      submit.disabled = false
      // Disabling the focused button sent the focus to <body>: bring it back where the user was
      if (document.activeElement === document.body) submit.focus()
    }
  }

  function setupCreate() {
    const form = $('#create-form')
    form.addEventListener('submit', (event) => {
      event.preventDefault()
      submitting(form, async () => {
        const created = await api('POST', '/api/redirects', formValues(form))
        form.reset()
        state.filter = ''
        $('#filter').value = ''
        state.highlightId = created.id
        await reload()
        setStatus(t('status.created', { name: created.name }))
        focusInItem(created.id, 'copy')
      })
    })
  }

  function setupEdit() {
    const dialog = $('#edit-dialog')
    const form = $('#edit-form')
    const slug = form.elements.slug
    const warning = $('#edit-slug-warning')

    slug.addEventListener('input', () => {
      const changed = state.editing !== null && slug.value.trim() !== state.editing.slug
      // Text only while it applies: the field's description never carries a warning that does not hold
      warning.textContent = changed ? t('form.slug.warning') : ''
    })
    form.addEventListener('submit', (event) => {
      event.preventDefault()
      const id = state.editing.id
      submitting(form, async () => {
        const saved = await api('PUT', `/api/redirects/${id}`, formValues(form))
        dialog.close()
        state.highlightId = saved.id
        await reload()
        setStatus(t('status.saved', { name: saved.name }))
        focusInItem(saved.id, 'edit')
      })
    })
    dialog.addEventListener('close', () => {
      const id = state.editing && state.editing.id
      state.editing = null
      if (id !== null) focusInItem(id, 'edit')
    })
  }

  function openEdit(redirect) {
    const dialog = $('#edit-dialog')
    const form = $('#edit-form')
    state.editing = redirect
    clearErrors(form)
    for (const name of fieldsOf(form)) form.elements[name].value = redirect[name]
    $('#edit-slug-warning').textContent = ''
    dialog.showModal()
    // Changing the target is the usual edit: land there, ready to paste
    form.elements.target.focus()
    form.elements.target.select()
  }

  function setupDelete() {
    const dialog = $('#delete-dialog')
    const confirm = $('#delete-confirm')
    confirm.addEventListener('click', async () => {
      const { redirect, place } = state.deleting
      confirm.disabled = true
      clearStatus(place)
      try {
        await api('DELETE', `/api/redirects/${redirect.id}`)
        setStatus(t('status.deleted', { name: redirect.name }), false, place)
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
        if (error.status !== 401) setStatus(error.message, true, place)
      } finally {
        confirm.disabled = false
        state.deleting = null
        dialog.close()
      }
      state.highlightId = null
      await reload().catch(() => setStatus(t('status.network'), true, place))
      $(place.title).focus()
    })
    dialog.addEventListener('close', () => {
      const deleting = state.deleting
      state.deleting = null
      if (deleting !== null) focusInItem(deleting.redirect.id, 'delete', deleting.place)
    })
  }

  // Any redirect is deleted the same way; `url` is the link that will stop working
  function openDelete(redirect, url, place) {
    state.deleting = { redirect, place }
    $('#delete-name').textContent = redirect.name
    $('#delete-text').textContent = t('delete.text', { url })
    $('#delete-dialog').showModal()
  }

  // --- NanoURLs ---------------------------------------------------------------------------------------

  function renderNanos() {
    const nanos = ofKind('nano')
    const list = $('#nano-list')
    list.replaceChildren(...nanos.map(renderItem))
    list.hidden = nanos.length === 0
    $('#nano-empty').hidden = nanos.length !== 0
    const count = $('#nano-count')
    count.textContent = nanos.length === 1 ? t('nano.count.one') : t('nano.count.other', { count: nanos.length })
    count.hidden = nanos.length === 0
  }

  function setupNanos() {
    const place = PLACES.nano
    const form = $('#nano-form')
    form.addEventListener('submit', (event) => {
      event.preventDefault()
      submitting(
        form,
        async () => {
          const created = await api('POST', '/api/nanourls', formValues(form))
          form.reset()
          state.highlightId = created.id
          await reload()
          setStatus(t('nano.created', { name: created.name, url: created.short_url }), false, place)
          focusInItem(created.id, 'copy', place)
        },
        place,
      )
    })

    const dialog = $('#nano-dialog')
    const editForm = $('#nano-edit-form')
    editForm.addEventListener('submit', (event) => {
      event.preventDefault()
      const id = state.editing.id
      submitting(
        editForm,
        async () => {
          const saved = await api('PUT', `/api/nanourls/${id}`, formValues(editForm))
          dialog.close()
          state.highlightId = saved.id
          await reload()
          setStatus(t('status.saved', { name: saved.name }), false, place)
          focusInItem(saved.id, 'edit', place)
        },
        place,
      )
    })
    dialog.addEventListener('close', () => {
      const id = state.editing && state.editing.id
      state.editing = null
      if (id !== null) focusInItem(id, 'edit', place)
    })

    $('#nano-list').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]')
      if (!button) return
      const nano = findRedirect(button.closest('[data-id]').dataset.id)
      if (!nano) return
      if (button.dataset.action === 'copy') copyLink(button, nano.short_url, place)
      else if (button.dataset.action === 'delete') openDelete(nano, nano.short_url, place)
      else if (button.dataset.action === 'edit') {
        state.editing = nano
        clearErrors(editForm)
        for (const name of fieldsOf(editForm)) editForm.elements[name].value = nano[name]
        dialog.showModal()
        editForm.elements.target.focus()
        editForm.elements.target.select()
      }
    })
  }

  // --- Login ------------------------------------------------------------------------------------------

  function showLogin() {
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close())
    state.appShown = false
    for (const selector of Object.values(VIEWS)) $(selector).hidden = true
    $('#tabs').hidden = true
    $('#logout').hidden = true
    $('#open-warning').hidden = true
    $('#load-error').hidden = true
    const form = $('#login-form')
    form.reset()
    setLoginError(null)
    $('#login-view').hidden = false
    form.elements.password.focus()
  }

  function setLoginError(message) {
    const input = $('#login-password')
    const slot = $('#login-password-error')
    slot.textContent = message || ''
    slot.hidden = !message
    if (message) input.setAttribute('aria-invalid', 'true')
    else input.removeAttribute('aria-invalid')
  }

  function setupLogin() {
    const form = $('#login-form')
    form.addEventListener('submit', async (event) => {
      event.preventDefault()
      const submit = $('[type="submit"]', form)
      submit.disabled = true
      setLoginError(null)
      try {
        await api('POST', '/api/login', { password: form.elements.password.value })
        form.reset()
        await showApp()
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
        setLoginError(error.message)
        form.elements.password.select()
      } finally {
        submit.disabled = false
        if (document.activeElement === document.body) form.elements.password.focus()
      }
    })
    $('#logout').addEventListener('click', async () => {
      await api('POST', '/api/logout').catch(() => {})
      showLogin()
    })
  }

  // --- API key ----------------------------------------------------------------------------------------

  function renderKey(info) {
    state.keyExists = info.exists
    $('#key-state').textContent = info.exists
      ? t('key.set', { date: info.created_at.slice(0, 10), hint: info.hint })
      : t('key.none')
    $('#key-generate').textContent = info.exists ? t('key.regenerate') : t('key.generate')
  }

  async function generateKey() {
    const button = $('#key-generate')
    button.disabled = true
    clearStatus()
    try {
      const created = await api('POST', '/api/key')
      renderKey({ exists: true, hint: created.hint, created_at: created.created_at })
      const value = $('#key-new-value')
      value.value = created.key
      $('#key-new-dialog').showModal()
      value.focus()
      value.select()
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      if (error.status !== 401) setStatus(error.message, true)
    } finally {
      button.disabled = false
    }
  }

  function setupKey() {
    $('#key-generate').addEventListener('click', () => {
      if (state.keyExists) $('#key-confirm-dialog').showModal()
      else generateKey()
    })
    $('#key-confirm').addEventListener('click', () => {
      $('#key-confirm-dialog').close()
      generateKey()
    })
    $('#key-new-copy').addEventListener('click', async (event) => {
      const button = event.currentTarget
      const copied = await copyText($('#key-new-value').value)
      button.focus()
      button.textContent = copied ? t('list.copied') : t('list.copy.failed')
      button.classList.toggle('is-copied', copied)
      button.classList.toggle('is-failed', !copied)
    })
    $('#key-new-dialog').addEventListener('close', () => {
      // The key is shown once: it does not stay in the page after the dialog
      $('#key-new-value').value = ''
      const copy = $('#key-new-copy')
      copy.textContent = t('key.new.copy')
      copy.classList.remove('is-copied', 'is-failed')
      if (!$('#key-card').hidden) $('#key-generate').focus()
    })
  }

  // --- Settings: hostname of the links, scripts' API ------------------------------------------------------

  function setSettingsError(message) {
    const input = $('#settings-hostname')
    const slot = $('#settings-hostname-error')
    slot.textContent = message || ''
    slot.hidden = !message
    if (message) input.setAttribute('aria-invalid', 'true')
    else input.removeAttribute('aria-invalid')
  }

  function renderSettings(settings) {
    state.settings = settings
    const form = $('#settings-form')
    form.elements.hostname.value = settings.hostname
    form.elements.hostname_enabled.checked = settings.hostname_enabled
    form.elements.hostname.disabled = !settings.hostname_enabled
    const mdns = $('#settings-mdns')
    mdns.textContent = settings.mdns ? t('settings.mdns', { hostname: settings.hostname }) : ''
    mdns.hidden = !settings.mdns
    const override = $('#settings-public-base-url')
    override.textContent = settings.public_base_url
      ? t('settings.public_base_url', { url: settings.public_base_url })
      : ''
    override.hidden = !settings.public_base_url

    const toggle = $('#api-enabled')
    toggle.checked = settings.api_enabled
    toggle.disabled = settings.api_forced
    const forced = $('#api-forced')
    const forcedText = settings.api_enabled ? t('api.forced_on') : t('api.forced_off')
    forced.textContent = settings.api_forced ? forcedText : ''
    forced.hidden = !settings.api_forced
    $('#api-off').hidden = settings.api_enabled
    $('#key-section').hidden = !settings.api_enabled
  }

  function saveSettings(values) {
    return api('PUT', '/api/settings', { ...state.settings, ...values }).then((saved) => {
      renderSettings(saved)
      return saved
    })
  }

  function setupSettings() {
    const form = $('#settings-form')
    form.elements.hostname_enabled.addEventListener('change', (event) => {
      form.elements.hostname.disabled = !event.target.checked
      $('#settings-saved').textContent = ''
    })
    form.elements.hostname.addEventListener('input', () => {
      $('#settings-saved').textContent = ''
    })
    form.addEventListener('submit', async (event) => {
      event.preventDefault()
      const submit = $('[type="submit"]', form)
      const formError = $('.form-error', form)
      submit.disabled = true
      setSettingsError(null)
      formError.hidden = true
      $('#settings-saved').textContent = ''
      try {
        await saveSettings({
          hostname: form.elements.hostname.value,
          hostname_enabled: form.elements.hostname_enabled.checked,
        })
        $('#settings-saved').textContent = t('settings.saved')
        await reload() // every link follows the new hostname
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
        if (error.status === 401) return
        if (error.errors.hostname) {
          setSettingsError(error.errors.hostname)
          form.elements.hostname.focus()
        } else {
          formError.textContent = error.message
          formError.hidden = false
        }
      } finally {
        submit.disabled = false
        if (document.activeElement === document.body) submit.focus()
      }
    })

    $('#api-enabled').addEventListener('change', async (event) => {
      const toggle = event.target
      const apiError = $('#api-error')
      apiError.hidden = true
      toggle.disabled = true
      try {
        const saved = await saveSettings({ api_enabled: toggle.checked })
        if (saved.api_enabled) renderKey(await api('GET', '/api/key'))
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
        if (error.status === 401) return
        toggle.checked = !toggle.checked
        apiError.textContent = error.message
        apiError.hidden = false
      } finally {
        toggle.disabled = state.settings.api_forced
        toggle.focus()
      }
    })
  }

  // --- QR codes ---------------------------------------------------------------------------------------

  const DEFAULT_DESIGN = {
    color: '#1b1f24',
    eye_color: '#1b1f24',
    background: '#ffffff',
    module_style: 'square',
    eye_style: 'square',
    logo: null,
    logo_size: 0.22,
    margin: 4,
  }

  function readDesign() {
    return {
      color: $('#qr-color').value,
      eye_color: $('#qr-eye-color').value,
      background: $('#qr-transparent').checked ? 'transparent' : $('#qr-background').value,
      module_style: $('#qr-module-style').value,
      eye_style: $('#qr-eye-style').value,
      logo: state.qrLogo,
      logo_size: Number($('#qr-logo-size').value) / 100,
      margin: Number($('#qr-margin').value),
    }
  }

  function showDesign(design) {
    $('#qr-color').value = design.color
    $('#qr-eye-color').value = design.eye_color
    $('#qr-transparent').checked = design.background === 'transparent'
    $('#qr-background').value = design.background === 'transparent' ? '#ffffff' : design.background
    $('#qr-module-style').value = design.module_style
    $('#qr-eye-style').value = design.eye_style
    $('#qr-logo-size').value = String(Math.round(design.logo_size * 100))
    $('#qr-margin').value = String(design.margin)
    $('#qr-logo').value = ''
    state.qrLogo = design.logo
    showDesignValues()
  }

  function showDesignValues() {
    $('#qr-logo-size-value').textContent = t('qr.percent', { value: $('#qr-logo-size').value })
    $('#qr-margin-value').textContent = t('qr.modules', { value: $('#qr-margin').value })
    $('#qr-logo-remove').hidden = !state.qrLogo
    $('#qr-logo-size').disabled = !state.qrLogo
    $('#qr-background').disabled = $('#qr-transparent').checked
  }

  function setDesignError(message) {
    const slot = $('#qr-design-error')
    slot.textContent = message || ''
    slot.hidden = !message
  }

  // The server draws the preview with the same code as the final SVG
  async function refreshPreview() {
    try {
      const { svg } = await api('POST', '/api/qrcodes/preview', {
        design: readDesign(),
        id: state.qrEditing ? state.qrEditing.id : null,
      })
      $('#qr-preview').src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
      setDesignError(null)
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      if (error.status !== 401) setDesignError(error.errors.design || error.message)
    }
  }

  function startCreating() {
    const form = $('#qr-form')
    state.qrEditing = null
    form.reset()
    clearErrors(form)
    showDesign(DEFAULT_DESIGN)
    $('#qr-form-title').textContent = t('qr.create.title')
    $('#qr-submit').textContent = t('qr.create.submit')
    $('#qr-cancel').hidden = true
  }

  function startEditing(qr) {
    const form = $('#qr-form')
    state.qrEditing = qr
    clearErrors(form)
    form.elements.name.value = qr.name
    form.elements.target.value = qr.target
    showDesign(qr.design)
    $('#qr-form-title').textContent = t('qr.edit.title')
    $('#qr-submit').textContent = t('form.edit.submit')
    $('#qr-cancel').hidden = false
    refreshPreview()
    form.elements.name.focus()
  }

  function renderQr(qr) {
    const item = $('#qr-template').content.firstElementChild.cloneNode(true)
    item.dataset.id = qr.id
    item.classList.toggle('is-new', qr.id === state.highlightId)
    const image = $('.qr-image', item)
    image.src = `${qr.svg_url}?v=${state.qrVersion}`
    image.alt = t('qr.image.alt', { name: qr.name })
    $('.item-name', item).textContent = qr.name
    const link = $('.link', item)
    link.href = qr.short_url
    link.replaceChildren(...breakableUrl(qr.short_url))
    $('.target-url', item).replaceChildren(...breakableUrl(qr.target))
    const download = $('[data-action="download"]', item)
    download.href = qr.svg_url
    download.download = `${qr.slug.split('/').pop()}.svg`
    download.setAttribute('aria-label', t('qr.download.aria', { name: qr.name }))
    $('[data-action="edit"]', item).setAttribute('aria-label', t('list.edit.aria', { name: qr.name }))
    $('[data-action="delete"]', item).setAttribute('aria-label', t('list.delete.aria', { name: qr.name }))
    return item
  }

  function renderQrs() {
    const list = $('#qr-list')
    list.replaceChildren(...state.qrcodes.map(renderQr))
    list.hidden = state.qrcodes.length === 0
    $('#qr-empty').hidden = state.qrcodes.length !== 0
    const count = $('#qr-count')
    const total = state.qrcodes.length
    count.textContent = total === 1 ? t('qr.count.one') : t('qr.count.other', { count: total })
    count.hidden = total === 0
    // The QR code being edited is gone (deleted here or elsewhere): back to creating one
    if (state.qrEditing && !state.qrcodes.some((qr) => qr.id === state.qrEditing.id)) {
      startCreating()
      refreshPreview()
    }
  }

  function setupQrs() {
    const place = PLACES.qr
    const form = $('#qr-form')
    for (const selector of ['#qr-color', '#qr-eye-color', '#qr-background', '#qr-transparent', '#qr-module-style', '#qr-eye-style', '#qr-logo-size', '#qr-margin']) {
      $(selector).addEventListener('change', () => {
        showDesignValues()
        refreshPreview()
      })
    }
    // Ranges show their value while they move; the preview follows on release
    for (const selector of ['#qr-logo-size', '#qr-margin']) $(selector).addEventListener('input', showDesignValues)
    $('#qr-logo').addEventListener('change', (event) => {
      const [file] = event.target.files
      if (!file) return
      const reader = new FileReader()
      reader.addEventListener('load', () => {
        state.qrLogo = reader.result
        showDesignValues()
        refreshPreview()
      })
      reader.readAsDataURL(file)
    })
    $('#qr-logo-remove').addEventListener('click', () => {
      state.qrLogo = null
      $('#qr-logo').value = ''
      showDesignValues()
      refreshPreview()
      $('#qr-logo').focus()
    })
    $('#qr-cancel').addEventListener('click', () => {
      const id = state.qrEditing && state.qrEditing.id
      startCreating()
      refreshPreview()
      focusInItem(id, 'edit', place)
    })
    form.addEventListener('submit', (event) => {
      event.preventDefault()
      setDesignError(null)
      submitting(
        form,
        async () => {
          const body = { name: form.elements.name.value, target: form.elements.target.value, design: readDesign() }
          const editing = state.qrEditing
          const saved = editing
            ? await api('PUT', `/api/qrcodes/${editing.id}`, body)
            : await api('POST', '/api/qrcodes', body)
          state.highlightId = saved.id
          startCreating()
          await reload()
          refreshPreview()
          const message = editing ? t('status.saved', { name: saved.name }) : t('qr.created', { name: saved.name })
          setStatus(message, false, place)
          focusInItem(saved.id, 'download', place)
        },
        place,
      )
    })
    $('#qr-list').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]')
      if (!button) return
      const qr = state.qrcodes.find((q) => q.id === button.closest('[data-id]').dataset.id)
      if (!qr) return
      if (button.dataset.action === 'edit') startEditing(qr)
      else if (button.dataset.action === 'delete') openDelete(qr, qr.short_url, place)
    })
    startCreating()
  }

  // --- Sections: redirects, log ------------------------------------------------------------------------

  const VIEWS = { redirects: '#main', nano: '#nano-view', qr: '#qr-view', logs: '#logs-view' }
  const VIEW_HASHES = { '#/nano': 'nano', '#/qr': 'qr', '#/logs': 'logs' }

  function currentView() {
    return VIEW_HASHES[location.hash] || 'redirects'
  }

  function showView() {
    const view = currentView()
    for (const [name, selector] of Object.entries(VIEWS)) $(selector).hidden = name !== view
    document.querySelectorAll('#tabs a').forEach((link) => {
      if (link.dataset.view === view) link.setAttribute('aria-current', 'page')
      else link.removeAttribute('aria-current')
    })
    if (view === 'logs') loadLogs(true)
    if (view === 'qr') refreshPreview()
  }

  // Texts of the log, one function per value so each key is written out in full
  const ACTION_TEXTS = {
    visit: () => t('logs.action.visit'),
    create: () => t('logs.action.create'),
    update: () => t('logs.action.update'),
    delete: () => t('logs.action.delete'),
    read: () => t('logs.action.read'),
  }
  const SOURCE_TEXTS = {
    link: () => t('logs.source.link'),
    interface: () => t('logs.source.interface'),
    api: () => t('logs.source.api'),
  }

  function cell(text, className) {
    const td = document.createElement('td')
    td.textContent = text ?? ''
    if (className) td.className = className
    return td
  }

  function logRow(event) {
    const row = document.createElement('tr')
    const redirect = cell(event.name)
    if (event.redirect_id) {
      const id = document.createElement('span')
      id.className = 'redirect-id'
      id.textContent = event.redirect_id
      redirect.append(id)
    }
    const browser = cell(event.user_agent, 'browser')
    if (event.user_agent) browser.title = event.user_agent
    row.append(
      cell(event.at.replace('T', ' ').replace('Z', ''), 'mono nowrap'),
      cell(ACTION_TEXTS[event.action] ? ACTION_TEXTS[event.action]() : event.action),
      cell(SOURCE_TEXTS[event.source] ? SOURCE_TEXTS[event.source]() : event.source),
      redirect,
      cell(event.path, 'mono path'),
      cell(event.status, event.status === 404 ? 'mono status-404' : 'mono'),
      cell(event.client, 'mono nowrap'),
      browser,
    )
    return row
  }

  async function loadLogs(reset) {
    const rows = $('#logs-rows')
    const more = $('#logs-more')
    const logsError = $('#logs-error')
    if (reset) {
      rows.replaceChildren()
      state.logsBefore = null
    }
    const params = new URLSearchParams({ limit: String(LOG_PAGE) })
    if (state.logsBefore !== null) params.set('before', String(state.logsBefore))
    if ($('#logs-filter').value) params.set('kind', $('#logs-filter').value)
    logsError.hidden = true
    try {
      const page = await api('GET', `/api/logs?${params}`)
      rows.append(...page.events.map(logRow))
      state.logsBefore = page.next
      more.hidden = page.next === null
      $('#logs-empty').hidden = rows.children.length > 0
      $('#logs-table').hidden = rows.children.length === 0
    } catch (error) {
      if (!(error instanceof ApiError)) throw error
      if (error.status === 401) return
      logsError.textContent = error.message
      logsError.hidden = false
    }
  }

  function setupLogs() {
    window.addEventListener('hashchange', () => {
      if (state.appShown) showView()
    })
    $('#logs-filter').addEventListener('change', () => loadLogs(true))
    $('#logs-more').addEventListener('click', () => loadLogs(false))
  }

  // --- Start ------------------------------------------------------------------------------------------

  function showLoadError() {
    for (const selector of Object.values(VIEWS)) $(selector).hidden = true
    $('#tabs').hidden = true
    const loadError = $('#load-error')
    loadError.textContent = t('status.load_failed')
    loadError.hidden = false
  }

  async function showApp() {
    $('#login-view').hidden = true
    $('#open-warning').hidden = state.loginRequired
    $('#logout').hidden = !state.loginRequired
    $('#key-card').hidden = !state.loginRequired
    try {
      await reload()
      renderSettings(await api('GET', '/api/settings'))
      if (state.loginRequired && state.settings.api_enabled) renderKey(await api('GET', '/api/key'))
      state.appShown = true
      $('#tabs').hidden = false
      showView()
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return // the login screen is shown
      showLoadError()
    }
  }

  async function start() {
    let auth
    try {
      auth = await api('GET', '/api/auth')
    } catch {
      showLoadError()
      return
    }
    state.loginRequired = auth.login_required
    if (auth.login_required && !auth.logged_in) showLogin()
    else await showApp()
  }

  // --- Wiring -----------------------------------------------------------------------------------------

  function setupList() {
    $('#list').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]')
      if (!button) return
      const redirect = findRedirect(button.closest('[data-id]').dataset.id)
      if (!redirect) return
      if (button.dataset.action === 'copy') copyLink(button, redirect.public_url)
      else if (button.dataset.action === 'edit') openEdit(redirect)
      else if (button.dataset.action === 'delete') openDelete(redirect, redirect.public_url, PLACES.link)
    })
    $('#filter').addEventListener('input', (event) => {
      state.filter = event.target.value
      render()
    })
    document.querySelectorAll('dialog [data-action="close"]').forEach((button) => {
      button.addEventListener('click', () => button.closest('dialog').close())
    })
  }

  async function init() {
    const response = await fetch('/static/strings.json')
    state.strings = await response.json()
    applyStrings(document)
    // Templates are outside the document: their texts are set once, before any copy is made
    document.querySelectorAll('template').forEach((template) => applyStrings(template.content))
    document.title = t('app.title')
    setupList()
    setupCreate()
    setupEdit()
    setupDelete()
    setupLogin()
    setupKey()
    setupSettings()
    setupLogs()
    setupNanos()
    setupQrs()
    await start()
  }

  init()
})()
