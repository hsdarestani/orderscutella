<?php
if (!defined('ABSPATH')) { exit; }

function cbb_chunk_tmp_dir() {
    $uploads = wp_upload_dir();
    $dir = trailingslashit($uploads['basedir']) . 'cutella-bot-bridge-tmp';
    if (!is_dir($dir)) { wp_mkdir_p($dir); }
    return $dir;
}
function cbb_chunk_id($payload) {
    $id = isset($payload['upload_id']) ? strtolower((string) $payload['upload_id']) : '';
    if (!preg_match('/^[a-f0-9]{32}$/', $id)) { cbb_fail('Invalid upload id.', 400); }
    return $id;
}
function cbb_chunk_path($id) { return trailingslashit(cbb_chunk_tmp_dir()) . $id . '.part'; }
function cbb_chunk_meta_key($id) { return 'cbb_upload_' . $id; }
function cbb_chunk_result_key($id) { return 'cbb_upload_result_' . $id; }
function cbb_chunk_decode($value) {
    $value = strtr((string) $value, '-_', '+/');
    $pad = strlen($value) % 4;
    if ($pad) { $value .= str_repeat('=', 4 - $pad); }
    return base64_decode($value, true);
}
function cbb_media_begin($payload) {
    $id = cbb_chunk_id($payload);
    $filename = sanitize_file_name(isset($payload['filename']) ? $payload['filename'] : 'cutella-product.jpg');
    $size = isset($payload['size']) ? intval($payload['size']) : 0;
    $sha256 = isset($payload['sha256']) ? strtolower((string) $payload['sha256']) : '';
    if ($size <= 0 || $size > 10 * 1024 * 1024) { cbb_fail('Invalid media size.', 400); }
    if (!preg_match('/^[a-f0-9]{64}$/', $sha256)) { cbb_fail('Invalid media hash.', 400); }
    $existing = get_transient(cbb_chunk_result_key($id));
    if (is_array($existing) && !empty($existing['id'])) {
        return array('upload_id'=>$id,'already_finished'=>true,'result'=>$existing);
    }
    $path = cbb_chunk_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) { throw new Exception('Could not initialize media upload.'); }
    if (!flock($fh, LOCK_EX)) { fclose($fh); throw new Exception('Could not lock media upload.'); }
    ftruncate($fh, 0); fflush($fh); flock($fh, LOCK_UN); fclose($fh);
    set_transient(cbb_chunk_meta_key($id), array('filename'=>$filename,'size'=>$size,'sha256'=>$sha256), HOUR_IN_SECONDS);
    return array('upload_id'=>$id,'ready'=>true);
}
function cbb_media_chunk($payload) {
    $id = cbb_chunk_id($payload);
    $meta = get_transient(cbb_chunk_meta_key($id));
    if (!is_array($meta)) { cbb_fail('Upload session expired.', 410); }
    $offset = isset($payload['offset']) ? intval($payload['offset']) : -1;
    $encoded = isset($payload['data']) ? (string) $payload['data'] : '';
    $bytes = cbb_chunk_decode($encoded);
    if ($offset < 0 || $bytes === false || $bytes === '') { cbb_fail('Invalid media chunk.', 400); }
    if ($offset + strlen($bytes) > intval($meta['size'])) { cbb_fail('Media chunk exceeds declared size.', 400); }
    $path = cbb_chunk_path($id);
    $fh = @fopen($path, 'c+b');
    if (!$fh) { throw new Exception('Could not open media upload.'); }
    if (!flock($fh, LOCK_EX)) { fclose($fh); throw new Exception('Could not lock media upload.'); }
    if (fseek($fh, $offset) !== 0) { flock($fh, LOCK_UN); fclose($fh); throw new Exception('Could not seek media upload.'); }
    $written = fwrite($fh, $bytes); fflush($fh); flock($fh, LOCK_UN); fclose($fh);
    if ($written !== strlen($bytes)) { throw new Exception('Could not write complete media chunk.'); }
    return array('upload_id'=>$id,'offset'=>$offset,'written'=>$written);
}

/**
 * Create an attachment from a file that is already on this server.
 *
 * media_handle_sideload() fires the normal attachment/image processing stack.
 * Some shared hosts have a partial PHP cURL installation where plugins hooked
 * into that stack call curl_multi_init(), causing the entire upload to fail.
 * The bridge therefore performs the local move + attachment insert itself and
 * deliberately suppresses after-insert hooks. Full metadata is generated only
 * when cURL multi is available; otherwise a safe minimal image metadata record
 * is stored so WooCommerce can use the original image normally.
 */
function cbb_register_local_media($path, $filename) {
    $uploads = wp_upload_dir();
    if (!empty($uploads['error'])) { throw new Exception((string)$uploads['error']); }
    if (!wp_mkdir_p($uploads['path'])) { throw new Exception('Could not create uploads directory.'); }

    $filename = wp_unique_filename($uploads['path'], sanitize_file_name($filename));
    $destination = trailingslashit($uploads['path']) . $filename;

    if (!@rename($path, $destination)) {
        if (!@copy($path, $destination)) { throw new Exception('Could not move uploaded media into WordPress uploads.'); }
        @unlink($path);
    }
    @chmod($destination, 0644);

    $checked = wp_check_filetype_and_ext($destination, $filename);
    $mime = !empty($checked['type']) ? (string)$checked['type'] : '';
    if ($mime === '') {
        $basic = wp_check_filetype($filename);
        $mime = !empty($basic['type']) ? (string)$basic['type'] : '';
    }
    if ($mime === '' || strpos($mime, 'image/') !== 0) {
        @unlink($destination);
        throw new Exception('Uploaded file is not a supported image.');
    }

    $title = sanitize_text_field(pathinfo($filename, PATHINFO_FILENAME));
    $attachment = array(
        'guid'           => trailingslashit($uploads['url']) . $filename,
        'post_mime_type' => $mime,
        'post_title'     => $title !== '' ? $title : 'Cutella product image',
        'post_content'   => '',
        'post_status'    => 'inherit',
    );

    // fire_after_hooks=false prevents third-party attachment hooks from
    // requiring unavailable curl_multi_* functions on constrained hosts.
    $attachment_id = wp_insert_attachment($attachment, $destination, 0, true, false);
    if (is_wp_error($attachment_id)) {
        @unlink($destination);
        throw new Exception($attachment_id->get_error_message());
    }

    $relative = _wp_relative_upload_path($destination);
    update_post_meta($attachment_id, '_wp_attached_file', $relative);
    $image_size = function_exists('wp_getimagesize') ? @wp_getimagesize($destination) : @getimagesize($destination);
    $minimal_metadata = array(
        'file'       => $relative,
        'width'      => is_array($image_size) && isset($image_size[0]) ? intval($image_size[0]) : 0,
        'height'     => is_array($image_size) && isset($image_size[1]) ? intval($image_size[1]) : 0,
        'sizes'      => array(),
        'image_meta' => array(),
    );

    if (function_exists('curl_multi_init')) {
        try {
            require_once ABSPATH . 'wp-admin/includes/image.php';
            $generated = wp_generate_attachment_metadata($attachment_id, $destination);
            if (is_array($generated) && !empty($generated)) {
                wp_update_attachment_metadata($attachment_id, $generated);
            } else {
                update_post_meta($attachment_id, '_wp_attachment_metadata', $minimal_metadata);
            }
        } catch (Throwable $e) {
            update_post_meta($attachment_id, '_wp_attachment_metadata', $minimal_metadata);
        }
    } else {
        update_post_meta($attachment_id, '_wp_attachment_metadata', $minimal_metadata);
    }

    return (int)$attachment_id;
}

function cbb_media_finish($payload) {
    $id = cbb_chunk_id($payload);
    $existing = get_transient(cbb_chunk_result_key($id));
    if (is_array($existing) && !empty($existing['id'])) { return $existing; }
    $meta = get_transient(cbb_chunk_meta_key($id));
    if (!is_array($meta)) { cbb_fail('Upload session expired.', 410); }
    $path = cbb_chunk_path($id);
    if (!is_file($path)) { cbb_fail('Uploaded media file not found.', 404); }
    clearstatcache(true, $path);
    if (filesize($path) !== intval($meta['size'])) { cbb_fail('Uploaded media size mismatch.', 409); }
    $actual_hash = hash_file('sha256', $path);
    if (!hash_equals((string)$meta['sha256'], (string)$actual_hash)) {
        @unlink($path); delete_transient(cbb_chunk_meta_key($id)); cbb_fail('Uploaded media checksum mismatch.', 409);
    }

    $attachment_id = cbb_register_local_media($path, (string)$meta['filename']);
    delete_transient(cbb_chunk_meta_key($id));
    $result = array('id'=>(int)$attachment_id,'source_url'=>(string)wp_get_attachment_url($attachment_id));
    set_transient(cbb_chunk_result_key($id), $result, HOUR_IN_SECONDS);
    return $result;
}
