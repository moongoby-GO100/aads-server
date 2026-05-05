<!--footer -->
<section class="footer">
  <div class="container">
    <div class="row">
      <div class="col-md-12">
        <ul class="footer_box">
          <h4>About <?php echo $shop['shop_name'];?></h4>
          <li>상호 : <?php echo $shop['shop_name'];?></li>
          <li>사업자 등록번호 : <?php echo $shop['shop_taxid'];?> </li>
          <li>주소 : <?php echo $shop['shop_num'];?></li>
          <li>전화 : <?php echo $shop['shop_tel'];?></li>
          <?php if($shop['shop_show_hp']) { ?>
          <li>핸드폰 : <?php echo $shop['shop_show_hp'];?></li>
          <?php } ?>
          <li>이메일: <?php echo $shop['shop_email'];?></li>
          <?php if($shop['kakao_id']) { ?>
          <li>카카오톡 : <?php echo $shop['kakao_id'];?></li>
          <?php } ?>
		  <?php if($shop['shop_wechat_id']) { ?>
          <li>위챗 ID : <?php echo $shop['shop_wechat_id'];?></li>
          <?php } ?>
          <?php if($shop['shop_account']) { ?>
          <li>계좌번호 : <?php echo $shop['shop_account'];?></li>
          <?php } ?>

        </ul>
      </div>
      <div class="col-md-12">
        <ul class="footer_box">
          <h4>고객 서비스</h4>
          <!--<li><a href="index.html"><img src="images/logo.png"  alt=""/></a></li>-->
          <li><a href="/main/company">회사 정보</a></li>
          <li><a href="/main/privacy">개인 정보 정책</a></li>
          <li><a href="/main/agreement">이용 약관</a></li>
        </ul>
      </div>


      <!--div class="col-md-3">
				<ul class="footer_box">
					<ul class="social">
					  <h4>Fllow us</h4>
					  <li class="facebook"><a href="#"><span> </span></a></li>
					  <li class="kas"><a href="#"><span> </span></a></li>
					  <li class="kat"><a href="#"><span> </span></a></li>
					  <li class="pinterest"><a href="#"><span> </span></a></li>
					  <li class="youtube"><a href="#"><span> </span></a></li>
					</ul>

				</ul>
			</div-->
    </div>
    <div class="row">
      <div class="col-md-12">
        <div class="footer_bottom">
          <div class="copy">
            <p>©copyright 2020 <?php echo $shop['shop_name'];?>. All rights reserved.</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>
<!-- end footer -->
<!-- ################################################################################################ -->
<a id="backtotop" href="#top"><i class="fa fa-chevron-up"></i></a>
<?php if($user_id && $username == $shop['user_id']) { ?>
<?
//<a href="javascript:webShare()" style="z-index: 999;
?>
<a href="javascript:product_new_kakao_msg_send(<?php echo $user_id?>)" style="z-index: 999;
    display: inline-block;
    position: fixed;
    visibility: hidden;
    bottom: 165px;
    right: 20px;
    width: 36px;
    height: 36px;
    line-height: 36px;
    font-size: 16px;
    text-align: center;
    opacity: 1;"><img src="<?php echo IMG_DIR;?>/share.png" style="visibility: visible;" alt="알림톡공유"></a>
<?php } ?>
<?php //if($is_mobile == 'Y' && $shop['kakao_link_url']) {?>
<!-- <a id="kakaolinkurl" href="<?php echo $shop['kakao_link_url'];?>" class="visible" style="opacity: 1;"><img src="<?php echo IMG_DIR;?>/kakao_icon.png" alt="카카오톡 연결"></a> -->
<?php //}?>

<?php // 2023.08.28 Add  // 도매업체 로그인시에는 아이콘 비노출
//if(explode(".",$_SERVER['HTTP_HOST'])[0] != $userid){
if($auth_code != '4'){
?>
<a id="kakaolinkurl" href="<?php echo $shop['kakao_link_url'];?>" class="visible" style="opacity: 1;"><img src="<?php echo IMG_DIR;?>/kakao_icon.png" alt="카카오톡 연결"></a>
<?php }?>


<script>
var auth_no = '<?php echo $auth_code;?>';
var mainNewGoodsTotal = parseInt("<?php echo $GoodsData2Total;?>");
var mainNewGoodsCnt = parseInt("<?php echo $shop['shop_goods_new_cnt'];?>");
var mainNewGoodsPage = 1;
var searchText = '<?php echo $SearchText;?>';
var category1 = '<?php echo $Category1;?>';

$(document).ready(function() {
  /*$(".mGoods-item").each(function() {
      $(this).on('mouseover touchstart', function(e) {
          //alert('touchstart');
  		$(this).parent().find('figcaption.mask').show();
          $(this).parent().find('ul.external').show();
          // $('ul.external').show();
  		// e.preventDefault();	//	이벤트취소
  	})
      .on('mouseout', function(e) {
          //alert('touchstart');
  		$(this).parent().find('figcaption.mask').hide();
          $(this).parent().find('ul.external').hide();
          // $('ul.external').show();
  		// e.preventDefault();	//	이벤트취소
  	});
  });*/

  $('#sForm')
    .on('submit', function() {
      //alert($(this).val());
      var cate1 = $(this).find('select[name="Category1"]').val();
      var cate2 = $(this).find('select[name="Category2"]').val();
      var cate3 = $(this).find('select[name="Category3"]').val();
      console.log(cate1);
      //return false;
    })
    .on('change', 'select[name="Category1"]', function() {
      //alert($(this).val());
      var val = $(this).val();
      var cate_code = val;
      var cate = $('#sForm select[name="Category2"]');
      cate.prop('disabled', true);

      $('#sForm select[name="Category3"]').html('').append('<option value="">소분류</option>');

      $(this).blur();

      if (cate_code)
        $.get("/goods/get_category/" + cate_code, function(data) {
          console.log(data);
          var opt_tag = '';
          var rtn = JSON.parse(data);
          console.log(rtn);
          cate.html('');

          if (rtn.length > 0) {
            cate.append('<option value="">중분류</option>');

            for (var i = 0; i < rtn.length; i++) {
              opt_tag += '<option value="' + rtn[i].Code + '|1">' + rtn[i].Name + '</option>';
            }
            cate.append(opt_tag);
          } else {
            opt_tag += '<option value="">중분류</option>';
            cate.append(opt_tag);
          }

          // console.log(opt_tag);
          //cate.append(opt_tag);
          cate.prop('disabled', false);
          $("#Category2").niceSelect("update");
        });
      else {
        cate.html('');
        cate.append('<option value="">중분류</option>');
        cate.prop('disabled', false);
      }
      $("#Category3").niceSelect("update");
    })
    .on('change', 'select[name="Category2"]', function() {
      var val = $(this).val();
      var res = val.split("|");
      var cate_code = res[0];
      var cate_depth = '';
      if (res[1]) cate_depth = parseInt(res[1]);
      var cate = $('#sForm select[name="Category3"]');
      cate.prop('disabled', true);

      $(this).blur();

      console.log(cate_code);
      if (cate_code)
        $.get("/goods/get_category/" + cate_code + "/" + cate_depth, function(data) {
          console.log(data);
          var opt_tag = '';
          var rtn = JSON.parse(data);
          cate.html('');
          console.log(rtn);

          if (rtn.length > 0) {
            cate.append('<option value="">소분류</option>');

            for (var i = 0; i < rtn.length; i++) {
              opt_tag += '<option value="' + rtn[i].Code + '|2">' + rtn[i].Name + '</option>';
            }

            cate.append(opt_tag);
          } else {
            opt_tag += '<option value="">소분류</option>';
            cate.append(opt_tag);
          }

          //console.log(opt_tag);
          //cate.append(opt_tag);
          cate.prop('disabled', false);
          $("#Category3").niceSelect("update");
        });
      else {
        cate.html('');
        cate.append('<option value="">소분류</option>');
        cate.prop('disabled', false);
      }
    });

  $('#search_button').on('click', function() {
    var text = $('#sForm2').find('input[name="search_text"]').val();
    console.log(text);
    if (!text) {
      alert("검색어를 입력하세요!");
      return false;
    }
    $('#sForm2').submit();
  });

  if (!searchText && category1) {
    var cate1_val = $('#sForm').find('select[name="Category1"] :selected').val();
    var cate2_val = $('#sForm').find('select[name="Category2"] :selected').val();
    var cate3_val = $('#sForm').find('select[name="Category3"] :selected').val();
    var cate1_name = $('#sForm').find('select[name="Category1"] :selected').text();
    var cate2_name = $('#sForm').find('select[name="Category2"] :selected').text();
    var cate3_name = $('#sForm').find('select[name="Category3"] :selected').text();
    console.log(cate1_name);
    var cate_text = cate1_name;
    if (cate2_val) cate_text += ' > ' + cate2_name;
    if (cate3_val) cate_text += ' > ' + cate3_name;
    $('#CateSearchText').text(cate_text);
  }

  $('#NewGoodsMoreBtn').on('click', function() {
    if (mainNewGoodsTotal <= (mainNewGoodsPage * mainNewGoodsCnt)) {
      alert('더이상 상품이 없습니다!');
      return;
    }

    mainNewGoodsPage += 1;

    if (mainNewGoodsPage > 1) {
      $.get("/main/paging/" + mainNewGoodsPage, function(data) {
        console.log(data);
        if (!data) {
          alert('상품을 가져오지 못했습니다!');
          return;
        }

        $('#NewGoodsList').append(data);
      });
    } else {
      alert('더이상 상품이 없습니다!');
      return;
    }
    $(this).blur();
  });
})

function product_new_kakao_msg_send(no) {
	let formData = new FormData();
	formData.append("no", no);

	$.ajax({
		type: 'post',
		url: '/goods/product_new_kakao_msg_send',
		data: formData,
		processData: false,
		contentType: false,
		dataType: 'json',
		success: function(data) {
			let userAgent = navigator.userAgent.toLowerCase();
			if (userAgent.match('newtalkapp')) {
				let app_data = {};
				app_data.url = '';
				app_data.title = data.info.data.msg;
				
				if (userAgent.match('iphone')) {
					app_data.mobile = 'ios';
				} else if (userAgent.match('ipad')) {
					app_data.mobile = 'ios';
				} else if (userAgent.match('ipod')) {
					app_data.mobile = 'ios';
				} else if (userAgent.match('android')) {
					app_data.mobile = 'android';
				} else {
					app_data.mobile = 'other';
				}
				
				if (app_data.mobile == 'ios') {
					webkit.messageHandlers.snsshare.postMessage(JSON.stringify(app_data));
				}
				else {
					window.location.href="snsshare://"+JSON.stringify(app_data);
				}
			}
			else {
				if ('canShare' in navigator) {
					try {
						navigator.share({
							url: '',
							title: data.info.data.msg
						}).then(() => {
							console.log('Thanks for sharing!');
						}).catch(console.error);
					} catch (err) {
						console.log(err.name, err.message);
					}
					return;
				}
			}
		},
		error: function(error) {
			console.log(error);
		},
		complete: function() {
			
		}
	});
}

function directShare() {
	navigator.share({
		title: 'My awesome post!',
		text: 'This post may or may not contain the answer to the universe',
		url: window.location.href
	}).then(() => {
		window.alert('Thanks for sharing!');
	})
	.catch(err => {
		window.alert(`Couldn't share because of`, err);
	});
}
function webShare() {
	if (navigator.share) {
		if (window.fetchEnabled) {
			let myRequest = new Request('https://mkonikov.com/images/instants.jpg');
			
			fetch(myRequest)
			.then(function(response) {
				if (!response.ok) {
					throw new Error('HTTP error, status = ' + response.status);
				}
				return response.blob();
			})
			.then(function(response) {
				directShare();
			})
		}
		else {
		directShare();
		}
	} else {
		window.alert('web share not supported');
	}
}
</script>

<script src="<?php echo JS_DIR; ?>/clipboard.min.js"></script>
<script>
var clipboard = new Clipboard("#copy-button");

clipboard.on('success', function(e) {
  alert('홍보 URL을 복사하였습니다!');
});
</script>

<?php if($is_mobile == 'Y') {?>
<script src="https://developers.kakao.com/sdk/js/kakao.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<!-- <script src="<?php echo JS_DIR;?>/kakaolink.js"></script> -->
<script>
// 사용할 앱의 Javascript 키를 설정해 주세요.
Kakao.init("eadf8be73fdee750e41725566405edbf");

function shareStory(url, text) {
  Kakao.Story.open({
    url: url,
    text: text,
  });
}

function kakaolink_send(text, url, img, img_w, img_h) {
  Kakao.Link.sendScrap({
    requestUrl: url
  });
}
</script>
<?php }?>

<script>
// SNS
function ddg_sns(id, url) {
  let userAgent = navigator.userAgent;

  switch (id) {
    case 'facebook':
        window.open(url, "win_facebook", "menubar=0,resizable=1,width=600,height=400");
      break;
      //case 'twitter'		: window.open(url, "win_twitter", "menubar=0,resizable=1,width=600,height=400"); break;
      //case 'googleplus'	: window.open(url, "win_googleplus", "menubar=0,resizable=1,width=600,height=600"); break;
      //case 'naverband'	: window.open(url, "win_naverband", "width=410, height=540, resizable=no"); break;
    case 'kakaostory':
      window.open(url, "win_kakaostory", "menubar=0,resizable=1,width=500,height=500");
      break;
    case 'naver':
      window.open(url, "win_naver", "width=410, height=540, scrollbars=0");
      break;
  }
  return false;
}
// down
function download_checked(val, id, code)
{
  console.log(auth_no);
  switch (val) {
    case 'Y':
      if (auth_no == '3') download_open(id, code);
      else if (auth_no == '4')
		  {
				if (userAgent.match('newtalkapp'))
				{
          download_ajax(id, code, 'app');
				}
				else {
          if (window.ReactNativeWebView)
            download_ajax(id, code, 'rnapp');
          else
            download_jszip(id, code, '/goods/goods_zip_urls');
				}
      }
      else download_jszip(id, code, '/goods/goods_zip_urls');
      break;
      // case 'N': alert('다운로드 권한이 없습니다!'); break;
    default:
      if (confirm('다운로드 권한이 없습니다.\n\n로그인후 마이 페이지의\n다운로드 이용권을 구매 후 다운로드가 가능합니다.\n\n로그인 페이지로 이동하겠습니까?')) {
        location.href = '/auth/login';
      }
      // alert('다운로드 권한이 없습니다!');
      break;
  }
  return false;
}

function download_jszip(goodsId, goodsCode, apiUrl) {
  if (typeof JSZip === 'undefined') {
    window.open('https://newtalk.kr/products/goods_code_zip_down?id=' + goodsId + '&code=' + goodsCode);
    return;
  }
  apiUrl = apiUrl || '/goods/goods_zip_urls';

  $.getJSON(apiUrl + '?code=' + goodsCode, function(data) {
    if (!data.success) {
      alert(data.msg || '다운로드 오류입니다.');
      return;
    }

    var zip = new JSZip();
    var total = data.images.length;
    var loaded = 0;
    var failed = [];
    var concurrency = 5;
    var cursor = 0;
    var active = 0;

    if (data.txt) zip.file(data.txt_name, data.txt);

    function fetchUrlWithRetry(url, retryLeft) {
      return fetch(url, {cache: 'no-store', credentials: 'same-origin'})
        .then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.arrayBuffer();
        })
        .catch(function(e) {
          if (retryLeft > 0) {
            return new Promise(function(resolve) {
              setTimeout(resolve, 500);
            }).then(function() {
              return fetchUrlWithRetry(url, retryLeft - 1);
            });
          }
          throw e;
        });
    }

    function fetchWithRetry(img, retryLeft) {
      return fetchUrlWithRetry(img.url, retryLeft)
        .catch(function(primaryError) {
          if (!img.fallback_url) throw primaryError;
          return fetchUrlWithRetry(img.fallback_url, 2)
            .catch(function(fallbackError) {
              throw new Error('primary: ' + (primaryError.message || primaryError) + ', fallback: ' + (fallbackError.message || fallbackError));
            });
        });
    }

    function logFailures() {
      if (!failed.length) return;
      var logUrl = apiUrl.replace('goods_zip_urls', 'goods_zip_download_log');
      $.ajax({
        url: logUrl,
        type: 'POST',
        data: {
          code: goodsCode,
          expected: total,
          success: total - failed.length,
          failed: JSON.stringify(failed)
        }
      });
    }

    function finishZip() {
      if (failed.length) {
        var guide = '이미지 다운로드 안내\n\n';
        guide += '상품코드: ' + goodsCode + '\n';
        guide += '전체 이미지: ' + total + '개\n';
        guide += '다운로드 성공: ' + (total - failed.length) + '개\n';
        guide += '다운로드 실패: ' + failed.length + '개\n\n';
        guide += '일부 이미지가 네트워크 또는 CDN 응답 지연으로 포함되지 않았습니다.\n';
        guide += '잠시 후 다시 다운로드하시면 누락 파일을 받을 수 있습니다.\n\n';
        guide += '누락 파일:\n';
        failed.forEach(function(item) {
          guide += '- ' + item.zip_path + ' (' + item.error + ')\n';
        });
        zip.file('다운로드_안내.txt', guide);
        logFailures();
      }
      if (total > 0 && failed.length === total) {
        alert('브라우저 직접 다운로드가 차단되어 기존 방식으로 다시 시도합니다.');
        window.open('https://newtalk.kr/products/goods_code_zip_down?id=' + goodsId + '&code=' + goodsCode);
        return;
      }
      zip.generateAsync({type: 'blob'}).then(function(blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = failed.length
          ? (data.partial_zip_name || ((data.goods_name || goodsCode) + '_partial_missing_' + failed.length + '.zip'))
          : (data.zip_name || (data.goods_name || goodsCode) + '.zip');
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(a.href);
        if (failed.length) alert('이미지 ' + failed.length + '개가 네트워크 응답 실패로 누락되었습니다. ZIP 안의 다운로드_안내.txt를 확인하고 잠시 후 다시 다운로드해 주세요.');
      });
    }

    function runQueue() {
      if (cursor >= total && active === 0) {
        finishZip();
        return;
      }
      while (active < concurrency && cursor < total) {
        (function(img) {
          active++;
          fetchWithRetry(img, 2)
            .then(function(buf) {
              zip.file(img.zip_path, buf);
            })
            .catch(function(e) {
              failed.push({
                url: img.url,
                zip_path: img.zip_path,
                error: e && e.message ? e.message : 'fetch failed'
              });
            })
            .then(function() {
              loaded++;
              active--;
              runQueue();
            });
        })(data.images[cursor++]);
      }
    }

    if (!total) {
      alert('다운로드할 이미지가 없습니다.');
      return;
    }
    runQueue();
  }).fail(function() {
    window.open('https://newtalk.kr/products/goods_code_zip_down?id=' + goodsId + '&code=' + goodsCode);
  });
}

function download_ajax(id, code, gb)
{
	var app_data = {}; // 픽업앱 전송 데이타(2021.11.09)

  if(!id && !code && !gb) {
    alert('정상적인 접근이 아닙니다.');
    return false;
  }

  $.ajax({
    url: '/goods/goods_code_zip_down',
    type: 'GET',
    data: {
      gb: 'ajax',
      id: id,
      code: code
    },
    success: function(data)
    {
      // LoadingModal.modal('hide');
      try {
        var rtn = JSON.parse(data);
        if (rtn.info['success'] == true)
        {
          // console.log(rtn.info);

          if (userAgent.match('iphone')) {
            app_data.mobile = 'ios';
          } else if (userAgent.match('ipad')) {
            app_data.mobile = 'ios';
          } else if (userAgent.match('ipod')) {
            app_data.mobile = 'ios';
          } else if (userAgent.match('android')) {
            app_data.mobile = 'android';
          } else {
            app_data.mobile = 'other';
          }

          app_data.url = "https://newtalk.kr/data/files/pick/" + encodeURIComponent(rtn.info['downfile']);

          // console.log(JSON.stringify(app_data));

          if(gb == 'rnapp')
          {
            // react native app test
            window.ReactNativeWebView.postMessage(
              JSON.stringify({ app_data: app_data })
            );
          }
          else {
            if(app_data.mobile == 'ios') {
              window.webkit.messageHandlers.download.postMessage(JSON.stringify(app_data));
            }else {
				//window.location.href="downimg://"+JSON.stringify(app_data);
				window.open(app_data.url);
            }
          }

          return;
        } else alert(rtn.info['text']);
      } catch (err) {
        alert(err);
        alert('처리 오류입니다.'); // err.message
        console.log(err);
      }
    },
    error: function(jqXHR, textStatus, errorThrown) {
      alert(errorThrown);
      // LoadingModal.modal('hide');
    }
  });

}

// down
// 오류 수정일(2021.07.08)
function download_open(id, code) {
  $.ajax({
    url: '/goods/down_plus_minus_goods_check/',
    type: 'POST',
    data: {
      goodsId: id
    },
    success: function(data) {
    console.log(data);
      try {
        var rtn = JSON.parse(data);
        // console.log(rtn);

        if (!rtn.info['success']) {
          alert('처리 오류(2)입니다.');
          return false;
        }

        if (rtn.info['success'] == true) {
          // 다운목록 상품이 아니면 확인창
          if (rtn.info['down_goods_check'] == 'N') {
            if (confirm('해당 상품은 회원님 다운상품 목록에 없는 상품입니다.\n\n확인을 누르면 다운상품에 담아지고 다운로드됩니다.\n\n진행하겠습니까?')) {
              window.open("http://newtalk.kr/products/goods_code_zip_down?id=" + id + "&code=" + code);
            }
            return false;
          }

          window.open("http://newtalk.kr/products/goods_code_zip_down?id=" + id + "&code=" + code);
        }
      } catch (err) {
        alert('처리 오류(1)입니다.'); // err.message
        console.log(err);
      }
    },
    error: function(jqXHR, textStatus, errorThrown) {
      alert(errorThrown);
    }
  });
}

const openShare = async (url, title) => {
  console.log(url, title);

	// 픽업앱 웹뷰이면
	if (userAgent.match('newtalkapp'))
	{
		var app_data = {};

		app_data.url = url;
		app_data.title = title;
		// console.log(userAgent);

		if (userAgent.match('iphone')) {
			app_data.mobile = 'ios';
		} else if (userAgent.match('ipad')) {
			app_data.mobile = 'ios';
		} else if (userAgent.match('ipod')) {
			app_data.mobile = 'ios';
		} else if (userAgent.match('android')) {
			app_data.mobile = 'android';
		} else {
			app_data.mobile = 'other';
		}

		console.log(JSON.stringify(app_data));

    if(app_data.mobile == 'ios') {
      webkit.messageHandlers.snsshare.postMessage(JSON.stringify(app_data));
    }else {
      window.location.href="snsshare://"+JSON.stringify(app_data);
    }
	}
	else
	{
		if ('canShare' in navigator) {
			try {
				// let imageResponse = await fetch(`https://api.taerin.site/storage/${postData.image}`);
				// let blob = await imageResponse.blob();
				// let file = new File([blob], "rick.jpg", { type: blob.type });

				// console.log(file);

				// if (window.navigator && window.navigator.canShare && window.navigator.canShare({ files: fileArray })) {
				//await navigator.share({
				navigator.share({
					url: url,
					// url: 'https://shop.newtalk.kr/goods/detail/35768',
					// files: [file],
					// files: [],
					title: title
					// text: ''
				}).then(() => {
					console.log('Thanks for sharing!');
				}).catch(console.error);
				// }
			} catch (err) {
				console.log(err.name, err.message);
			}
			return;
		}
		else alert('공유기능을 지원하지 않는 환경입니다.');
	}
};
</script>


<script>
// 스크립 by 조용학 200701
$(document).ready(function() {

  // 셀렉트 박스 디자인
  $(".select-design").niceSelect();
  $("#Category1").change(function() {
    // setTimeout(function(){
    // 	$("#Category2").niceSelect("update");
    // },100);
  });
  $("#Category2").change(function() {
    // setTimeout(function(){
    // 	$("#Category3").niceSelect("update");
    // },100);
  });

  // 헤더 검색 기능
  $(".search-btn").on("click", function() {
    if ($(this).hasClass("on")) {
      $(this).removeClass("on");
      $("#search").removeClass("on");
    } else {
      $(this).addClass("on");
      $("#search").addClass("on");
    }
  });

  // 헤더 이미지 2단 기능
  $(".change-btn02").click(function() {
    $(this).addClass("on").siblings().removeClass("on");
    $(".layout-type").addClass("layout-type01");
    $(".layout-type").removeClass("layout-type02");
  });

  // 헤더 이미지 3단 기능
  $(".change-btn01").click(function() {
    $(this).addClass("on").siblings().removeClass("on");
    $(".layout-type").addClass("layout-type02");
    $(".layout-type").removeClass("layout-type01");
  });

  // 메인 팝업
  $(".close-pop").on("click", function() {
    $(".modal-pop").fadeOut();
  });

  // 메인 이미지 2단 기능
  $(".change-btn04").click(function() {
    $(this).addClass("on").siblings().removeClass("on");
    $(this).parents(".layout-type").addClass("layout-type01");
    $(this).parents(".layout-type").removeClass("layout-type02");
  });

  // 메인 이미지 3단 기능
  $(".change-btn03").click(function() {
    $(this).addClass("on").siblings().removeClass("on");
    $(this).parents(".layout-type").addClass("layout-type02");
    $(this).parents(".layout-type").removeClass("layout-type01");
  });

  // 제품이미지 모바일 클릭 기능
  $(".mGoods-item").on("click", function() {
    $(".mGoods-item .mobile-overlay").show();
    $(".mGoods-item").children(".mask,.external").removeClass("on");
    $(this).children(".mobile-overlay").hide();
    $(this).children(".mask,.external").addClass("on");
  });

  if (matchMedia("screen and (max-width: 767px)").matches) {
    $("#NewGoodsList").removeClass("layout-type01").addClass("layout-type02");
    $("#NewGoodsList .change-btn04").removeClass("on");
    $("#NewGoodsList .change-btn03").addClass("on");
  }

  // PC 우클릭 금지 기능
  document.onmousedown = disableclick;
  status = "우클릭 금지입니다.";

  function disableclick(event) {
    if (event.button == 2) {
      alert(status);
      return false;
    }
  }

  //안드로이드 우측 버튼 비활성
  $(document).bind("contextmenu", function(e) {
    return false;
  });
});
</script>

</body>
</html>
