<?php echo $link_tag1;?>
<?php echo $link_tag2;?>
<?php echo $link_tag3;?>

<style>
.modal-header,
h4,
.close {
  background-color: #5cb85c;
  color: white !important;
  text-align: center;
  font-size: 25px;
}

.modal-footer {
  background-color: #f9f9f9;
}

#gview_jqGrid {  overflow: hidden; }

.ui-jqgrid tr.ui-jqgrid-labels th {
  background-color: #e8e8e8;
}

.ui-jqgrid tbody tr:hover {
  background-color: #e8e8e8;
}

.ui-jqgrid tr.jqgrow td {
  outline-style: none;
  color: #286abf;
  font-weight: normal;
  cursor: pointer;
  vertical-align: middle !important
}

.ui-jqgrid tr.jqgrow td {
  word-wrap: break-word;
  /* IE 5.5+ and CSS3 */
  white-space: pre-wrap;
  /* CSS3 */
  white-space: -moz-pre-wrap;
  /* Mozilla, since 1999 */
  white-space: -pre-wrap;
  /* Opera 4-6 */
  white-space: -o-pre-wrap;
  /* Opera 7 */
  overflow: hidden;
  height: auto;
  vertical-align: middle;
  padding-top: 3px;
  padding-bottom: 3px;
}

/*.ui-jqgrid tr.jqgrow td { white-space: normal !important; height: auto; vertical-align: text-top; padding-top: 2px; }*/
th.ui-th-column div {
  word-wrap: break-word;
  /* IE 5.5+ and CSS3 */
  white-space: pre-wrap;
  /* CSS3 */
  white-space: -moz-pre-wrap;
  /* Mozilla, since 1999 */
  white-space: -pre-wrap;
  /* Opera 4-6 */
  white-space: -o-pre-wrap;
  /* Opera 7 */
  overflow: hidden;
  height: auto;
  vertical-align: middle;
  padding-top: 3px;
  padding-bottom: 3px;
}

.ui-jqgrid .ui-search-table {
  text-align: center;
  width: 90%;
}

.table>tbody>tr>td,
.table>tfoot>tr>td,
.table>thead>tr>td {
  padding: 2px 2px 2px 2px;
}

.ui-jqgrid td input {
  margin: 0 4px 0 4px;
}

.ui-jqgrid .ui-search-table td.ui-search-clear {
  display: none;
}

.ui-paging-info {
  padding-right: 20px;
}

.table>thead>tr>td.success,
.table>tbody>tr>td.success,
.table>tfoot>tr>td.success,
.table>thead>tr>th.success,
.table>tbody>tr>th.success,
.table>tfoot>tr>th.success,
.table>thead>tr.success>td,
.table>tbody>tr.success>td,
.table>tfoot>tr.success>td,
.table>thead>tr.success>th,
.table>tbody>tr.success>th,
.table>tfoot>tr.success>th {
  background-color: #e8e8e8;
}

.callout.callout-info {
  background-color: #00c0ef !important;
  color: #fff !important;
  border-color: #0097bc;
}

.callout {
  border-radius: 3px;
  margin: 0 0 20px 0;
  padding: 15px 30px 15px 15px;
  border-left: 5px solid #eee;
}

.callout h4 {
  background-color: #00c0ef !important;
  color: white !important;
  text-align: left;
  font-size: 20px;
}

.panel-heading p {
  margin: 5px 0 0;
}

.panel-heading .bold {
  color: #555555;
  font-weight: bold;
  font-size: 1.5rem;
}
.btn {
  padding-left: 0.75rem;
  padding-right: 0.75rem;
}

label.btn {
  margin-bottom: 0;
}

.d-flex > .btn {
  flex: 1;
}

.carbonads {
  border: 1px solid #ccc;
  border-radius: 0.25rem;
  font-size: 0.875rem;
  overflow: hidden;
  padding: 1rem;
}

.carbon-wrap {
  overflow: hidden;
}

.carbon-img {
  clear: left;
  display: block;
  float: left;
}

.carbon-text,
.carbon-poweredby {
  display: block;
  margin-left: 140px;
}

.carbon-text,
.carbon-text:hover,
.carbon-text:focus {
  color: #fff;
  text-decoration: none;
}

.carbon-poweredby,
.carbon-poweredby:hover,
.carbon-poweredby:focus {
  color: #ddd;
  text-decoration: none;
}

@media (min-width: 768px) {
  .carbonads {
    float: right;
    margin-bottom: -1rem;
    margin-top: -1rem;
    max-width: 360px;
  }
}

.footer {
  font-size: 0.875rem;
}

.heart {
  color: #ddd;
  display: block;
  height: 2rem;
  line-height: 2rem;
  margin-bottom: 0;
  margin-top: 1rem;
  position: relative;
  text-align: center;
  width: 100%;
}

.heart:hover {
  color: #ff4136;
}

.heart::before {
  border-top: 1px solid #eee;
  content: " ";
  display: block;
  height: 0;
  left: 0;
  position: absolute;
  right: 0;
  top: 50%;
}

.heart::after {
  background-color: #fff;
  content: "♥";
  padding-left: 0.5rem;
  padding-right: 0.5rem;
  position: relative;
  z-index: 1;
}

.docs-demo {
  margin-bottom: 1rem;
  overflow: hidden;
  padding: 2px;
}

.img-container,
.img-preview {
  background-color: #f7f7f7;
  text-align: center;
  width: 100%;
}

.img-container {
  max-height: 497px;
  min-height: 200px;
}

@media (min-width: 768px) {
  .img-container {
    min-height: 497px;
  }
}

.img-container > img {
  max-width: 100%;
}

.docs-preview {
  margin-right: -1rem;
}

.img-preview {
  float: left;
  margin-bottom: 0.5rem;
  margin-right: 0.5rem;
  overflow: hidden;
}

.img-preview > img {
  max-width: 100%;
}

.preview-lg {
  height: 9rem;
  width: 16rem;
}

.preview-md {
  height: 4.5rem;
  width: 8rem;
}

.preview-sm {
  height: 2.25rem;
  width: 4rem;
}

.preview-xs {
  height: 1.125rem;
  margin-right: 0;
  width: 2rem;
}

.docs-data > .input-group {
  margin-bottom: 0.5rem;
}

.docs-data .input-group-prepend .input-group-text {
  min-width: 4rem;
}

.docs-data .input-group-append .input-group-text {
  min-width: 3rem;
}

.docs-buttons > .btn,
.docs-buttons > .btn-group,
.docs-buttons > .form-control {
  margin-bottom: 0.5rem;
  margin-right: 0.25rem;
}

.docs-toggles > .btn,
.docs-toggles > .btn-group,
.docs-toggles > .dropdown {
  margin-bottom: 0.5rem;
}

.docs-tooltip {
  display: block;
  margin: -0.5rem -0.75rem;
  padding: 0.5rem 0.75rem;
}

.docs-tooltip > .icon {
  margin: 0 -0.25rem;
  vertical-align: top;
}

.tooltip-inner {
  white-space: normal;
}

.btn-upload .tooltip-inner,
.btn-toggle .tooltip-inner {
  white-space: nowrap;
}

.btn-toggle {
  padding: 0.5rem;
}

.btn-toggle > .docs-tooltip {
  margin: -0.5rem;
  padding: 0.5rem;
}

@media (max-width: 400px) {
  .btn-group-crop {
    margin-right: -1rem !important;
  }

  .btn-group-crop > .btn {
    padding-left: 0.5rem;
    padding-right: 0.5rem;
  }

  .btn-group-crop .docs-tooltip {
    margin-left: -0.5rem;
    margin-right: -0.5rem;
    padding-left: 0.5rem;
    padding-right: 0.5rem;
  }
}

.docs-options .dropdown-menu {
  width: 100%;
}

.docs-options .dropdown-menu > li {
  font-size: 0.875rem;
  padding: 0.125rem 1rem;
}

.docs-options .dropdown-menu .form-check-label {
  display: block;
}

.docs-cropped .modal-body {
  text-align: center;
}

.docs-cropped .modal-body > img,
.docs-cropped .modal-body > canvas {
  max-width: 100%;
}
</style>

<div id="page-wrapper">
  <div class="row">
    <div class="col-xs-12 col-sm-12 col-lg-12">
      <h3 class="page-header"><i class="fa fa-th-list"></i> 상품목록</h3>

      <?php if($goods_cnt > 0) {?>
      <div class="callout callout-info">
        <h4><i class="fa fa-asterisk"></i> 촬영가능수량 : <span id="GoodsUseTotalCnt"></span>개</h4>
        <span id="GoodsCntTxt"></span>
      </div>
      <?php }?>

      <div class="panel panel-default">
        <div class="panel-heading">
          <div class="btn-group">
            <button type="button" class="btn btn-default dropdown-toggle" data-toggle="dropdown" aria-expanded="false">선택형 매뉴 <span class="caret"></span></button>
            <ul class="dropdown-menu" role="menu">
              <li gb="2"><a href="javascript://"><i class="fas fa-eye"></i> <span class="text-success"> 미니몰 노출</span></a></li>
              <li gb="3"><a href="javascript://"><i class="fas fa-eye-slash"></i> <span class="text-danger"> 미니몰 해제</span></a></li>
              <li class="divider"></li>
              <li gb="4"><a href="javascript://"><i class="fa fa-plus-square"></i> <span class="text-success"> 베스트상품 담기</span></a></li>
              <li gb="5"><a href="javascript://"><i class="fa fa-minus-square"></i> <span class="text-danger"> 베스트상품 해제</span></a></li>
              <li class="divider"></li>
              <li gb="6"><a href="javascript://"><i class="fas fa-download"></i> <span class="text-success"> 상품 다운로드</span></a></li>
            </ul>
            <?php //if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") { ?>
            <button type="button" class="btn btn-warning" onclick="showMallGoodsThumbnailModal()">도매몰 상품이미지 썸네일 수정</span>
            <?php //} ?>
          </div>
          <p class="text-danger"><span class="bold">* 정렬</span> : 미니도매몰 메인페이지 'NEW ITEMS' 상품에 적용되는 숫자이며 높은 수치일수록 상위에
            노출됩니다.</p>
          <p class="text-danger"><span class="bold">* 정렬</span> : 동일한 수치일때는 최신 등록일 순으로 적용됩니다.</p>
          <p class="text-danger"><span class="bold">* 정렬 / 도매몰판매가</span> : 해당 값에 클릭하면 수정필드로 변경되며 값을 수정 후 엔터(Enter) 키로
            반영됩니다.</p>
        </div>
        <div class="table-responsive" style="overflow: hidden;">
          <section>
            <table id="jqGrid" class="table"></table>
            <div id="jqGridPager"></div>
          </section>
        </div>
        <div class="panel-footer">
          <div class="btn-group dropup">
            <button type="button" class="btn btn-default dropdown-toggle" data-toggle="dropdown" aria-expanded="false">선택형 매뉴 <span class="caret"></span></button>
            <ul class="dropdown-menu" role="menu">
              <li gb="2"><a href="javascript://"><i class="fas fa-eye"></i> <span class="text-success"> 미니몰 노출</span></a></li>
              <li gb="3"><a href="javascript://"><i class="fas fa-eye-slash"></i> <span class="text-danger"> 미니몰 해제</span></a></li>
              <li class="divider"></li>
              <li gb="4"><a href="javascript://"><i class="fa fa-plus-square"></i> <span class="text-success"> 베스트상품 담기</span></a></li>
              <li gb="5"><a href="javascript://"><i class="fa fa-minus-square"></i> <span class="text-danger"> 베스트상품 해제</span></a></li>
              <li class="divider"></li>
              <li gb="6"><a href="javascript://"><i class="fas fa-download"></i> <span class="text-success"> 상품 다운로드</span></a></li>
            </ul>
          </div>
        </div>
      </div>
    </div>
    <!-- /.col-lg-12 -->
    <div class="col-xs-0 col-sm-0 col-lg-0"></div>
  </div>
</div>
<!-- /#page-wrapper -->

<div class="modal fade" id="alertModal">
  <div class="modal-dialog modal-sm">
    <div class="modal-content">
      <div class="modal-header" style="padding:35px 50px;">
        <button type="button" class="close" data-dismiss="modal" aria-hidden="true">&times;</button>
        <h4 class="modal-title"><span class="glyphicon glyphicon-lock"></span> 경고창</h4>
      </div>
      <div class="modal-body">
        <div class="text-left">
          <h5></h5>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="LoadingModal" class="modal" tabindex="-1" role="dialog" data-keyboard="false" data-backdrop="static">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header" style="text-align: center">
        <h3>처리중..</h3>
      </div>
      <div class="modal-body">
        <div style="height:200px">
          <span id="loading_spinner_center" style="position: absolute;display: block;top: 50%;left: 50%;"></span>
        </div>
      </div>
      <div class="modal-footer" style="text-align: center">상품 썸네일 이미지가 없으면 생성합니다.<br>잠시만 기달려 주세요.</div>
    </div>
  </div>
</div>


<div class="modal fade" id="MallGoodsThumbnailModal">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header" style="padding:35px 50px;">
        <button type="button" class="close" data-dismiss="modal" aria-hidden="true">&times;</button>
        <h4 class="modal-title"><i class="fas fa-edit"></i> 도매몰 상품이미지 썸네일 수정</h4>
      </div>
      <div class="modal-body">
        <!-- /.panel -->
        <div class="panel panel-default">
          <div class="panel-heading">
            <h6>
              <p class="text-danger">* 저장 파일명과 확장자(jpg)는 변경되지 않습니다.</p>
              <p class="text-danger">* 이미지에 + 커서 표시는 자르기가 가능합니다.</p>
            </h6>
          </div>
          <!-- /.panel-heading -->
          <div class="panel-body">
            <form id="MallGoodsThumbnailForm" class="form-horizontal" method="post" role="form">
				    <input type="hidden" name="goodsCode">

              <div class="tab-content">
                <div class="row">
                  <div class="col-md-12">
                    <div class="docs-data">
                      <div class="col-md-6">
                        <div class="form-group">
                          <label for="dataWidth">Width(px): <span class="width"></span></label>
                          <input type="text" class="form-control" id="dataWidth" placeholder="width" disabled>
                        </div>
                      </div>
                      <div class="col-md-6">
                        <div class="form-group">
                          <label for="dataHeight">Height(px): <span class="height"></span></label>
                          <input type="text" class="form-control" id="dataHeight" placeholder="height" disabled>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div class="col-md-12">
                    <div class="docs-demo">
                      <div class="img-container">
                        <img src="" alt="Picture" class="cropper-hidden">
                      </div>
                    </div>
                  </div>
                </div>

                <div class="row" id="actions">
                  <div class="col-md-12 docs-buttons">
                    <div class="btn-group">
                      <button type="button" class="btn btn-primary" data-method="clear" title="리셋">
                        <span class="docs-tooltip" data-toggle="tooltip" title="" data-original-title="리셋">
                          <span class="fa fa-sync-alt"></span>
                        </span>
                      </button>
                      <label class="btn btn-primary btn-upload" for="inputImage" title="이미지 업로드">
                        <input type="file" class="sr-only" id="inputImage" name="croppedImage" accept="image/*">
                        <span class="docs-tooltip" data-toggle="tooltip" title="" data-original-title="이미지 업로드">
                          <span class="fa fa-upload"></span>
                        </span>
                      </label>
                    </div>
                    <div class="btn-group btn-group-crop">
                      <button type="button" class="btn btn-success" data-method="getCroppedCanvas" data-option="{ &quot;maxWidth&quot;: 4096, &quot;maxHeight&quot;: 4096 }">
                        <span class="docs-tooltip" data-toggle="tooltip" title="" data-original-title="다운">썸네일 이미지 다운로드</span>
                      </button>
                    </div>
                    <div class="btn-group btn-group-crop">
                      <button type="button" class="btn btn-danger" data-method="save">
                        <span class="docs-tooltip" data-toggle="tooltip" title="" data-original-title="저장">썸네일 이미지 저장하기</span>
                      </button>
                    </div>
                  </div>
                </div>

              </div>
            </form>
          </div>
          <!-- /.panel-body -->
        </div>
        <!-- /.panel -->
      </div>
      <!-- <div class="modal-footer">등록일 : 최종수정일 : </div> -->
    </div>
  </div>
</div>

<!-- Show the cropped image in modal -->
<div class="modal fade docs-cropped" id="getCroppedCanvasModal" role="dialog" aria-hidden="true" aria-labelledby="getCroppedCanvasTitle" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h4 class="modal-title" id="getCroppedCanvasTitle">다운로드 이미지</h4>
        <button type="button" class="close" data-dismiss="modal" aria-label="Close">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-dismiss="modal">Close</button>
        <a class="btn btn-primary" id="download" href="javascript:void(0);" download="cropped.jpg">다운로드</a>
      </div>
    </div>
  </div>
</div><!-- /.modal -->

<script type="text/javascript" language="javascript" src="/include/jqgrid/i18n/grid.locale-kr.js"></script>
<script type="text/javascript" language="javascript" src="/include/jqgrid/jquery.jqGrid.min.js"></script>
<script src="/assets/js/spin.min.js"></script>
<script src="/assets/js/cropper.min.js"></script>
<script src="/assets/js/jquery-cropper.min.js"></script>
<script src="//developers.kakao.com/sdk/js/kakao.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>

<script type='text/javascript'>
//<![CDATA[
var token = '';
var storyCnt = 1;
var storyLastId = '';
var LoadingModal = $('#LoadingModal');
var MallGoodsThumbnailModal = $('#MallGoodsThumbnailModal');
var today = new Date();

// 사용할 앱의 JavaScript 키를 설정해 주세요.
Kakao.init('<?php echo $sns_key;?>');

function loginWithKakao(goodsId) {
  // 로그인 창을 띄웁니다.
  Kakao.Auth.login({
    success: function(authObj) {
      //alert(JSON.stringify(authObj));
      token = authObj.access_token;
      console.log(token);
      console.log(goodsId);

      //LoadingModal.modal('show');
      goods_post(token, goodsId);
    },
    fail: function(err) {
      alert(JSON.stringify(err));
    }
  });
};

function goods_post(access_token, goodsId) {
  if (!token) token = access_token;

  //alert('점검중입니다!');
  //return;

  LoadingModal.modal('show');

  $.get("/kakao/goods_post/" + goodsId + "/" + token, function(data) {
    try {
      var sendGoodsInnerHtml = '';
      //$('#view').append(data+'<br>');
      console.log(data);
      var rtn = JSON.parse(data);
      console.log(rtn);

      LoadingModal.modal('hide');
      if (rtn[0].PostResult.id === undefined) {
        alert('포스팅에 실패했습니다!');
        return false;
      }
      alert('포스팅 되었습니다.');
    } catch (err) {
      LoadingModal.modal('hide');
      console.log(err);
      alert('처리 오류입니다.'); // err.message
    }
  });
}
//]]>
</script>

<script type="text/javascript" language="javascript">
var goods_cnt = '<?php echo $goods_cnt;?>';

$(document).ready(function() {
  var url = '/products/goods_master_ajax_list';
  var wish = '<?php echo $wishing;?>';
  if (wish == 'best')
    url = '/products/goods_master_ajax_list/best';

  //$.jgrid.defaults.width = 780;
  $.jgrid.defaults.responsive = true;
  $.jgrid.defaults.styleUI = 'Bootstrap';

  $("#jqGrid").jqGrid({
    url: url,
    editurl: '/products/index_edit',
    mtype: "POST",
    datatype: "json",
    //width: 780,
    height: 600,
    colModel: [{
        label: '상품일련번호',
        name: 'GoodsId',
        key: true,
        hidden: true,
        editable: false,
        search: false
      },
      {
        label: '마스터일련번호',
        name: 'GdsMstId',
        key: true,
        hidden: true,
        editable: false,
        search: false
      },
      {
        label: '정렬',
        name: 'GoodsEtc6Sort',
        editable: true,
        width: 70,
        align: 'center',
        formatter: 'integer',
        search: false
      },
      {
        label: '메인',
        name: 'GoodsImage',
        width: 60,
        height: 60,
        align: 'center',
        formatter: formatImage,
        sortable: false,
        search: false
      },
      {
        label: '다운로드',
        name: 'Download',
        editable: false,
        width: 70,
        align: 'center',
        formatter: GoodsCodeValue,
        sortable: false,
        search: false
      },
      {
        label: '모델명(사입명)',
        name: 'GoodsName',
        editable: false,
        width: 200,
        formatter: formatGoodsName,
        search: true
      }, // '100%'
      {
        label: '상품코드',
        name: 'GoodsCode',
        editable: false,
        width: 120,
        align: 'center',
        search: true
      },
      {
        label: '노출',
        name: 'activated',
        width: 80,
        align: 'center',
        sortable: true,
        search: true,
        stype: 'select',
        searchoptions: {
          sopt: ['eq'],
          value: ':전체;Y:Y;N:N'
        }
      },
      {
        label: '미니몰',
        name: 'mall_activated',
        width: 80,
        align: 'center',
        sortable: true,
        search: true,
        stype: 'select',
        searchoptions: {
          sopt: ['eq'],
          value: ':전체;Y:Y;N:N'
        }
      },
      {
        label: '상품상태',
        name: 'GoodsEtc52',
        width: 100,
        align: 'center',
        sortable: true,
        search: true,
        stype: 'select',
        searchoptions: {
          sopt: ['eq'],
          value: ':전체;1:대기중;2:공급중;3:일시중지;4:완전품절;5:미사용;6:삭제;7:자료없음'
        }
      },
      {
        label: 'B상품',
        name: 'checking',
        width: 80,
        height: 50,
        align: 'center',
        sortable: false,
        search: false
      },
      {
        label: '원가',
        name: 'GoodsEtc9',
        editable: false,
        width: 100,
        align: 'center',
        formatter: 'integer',
        formatoptions: {
          defaultValue: 'No Value Set',
          thousandsSeparator: ','
        },
        search: false
      },
      {
        label: '도매몰판매가',
        name: 'GoodsEtc42',
        editable: true,
        width: 100,
        align: 'center',
        formatter: 'integer',
        formatoptions: {
          defaultValue: 'No Value Set',
          thousandsSeparator: ','
        },
        search: false
      },
      {
        label: '최초등록일',
        name: 'created',
        width: 160,
        align: 'center',
        search: false
      },
      {
        label: '재등록일',
        name: 're_created',
        width: 160,
        align: 'center',
        formatter: formatCreated,
        search: false,
      },
      {
        label: 'SNS포스팅',
        name: 'SnsPost',
        key: false,
        width: 90,
        align: 'center',
        editable: false,
        sortable: false,
        formatter: SnsPostValue,
        search: false
      },
      {
        label: '추가1',
        name: 'GoodsImage1',
        width: 60,
        height: 60,
        align: 'center',
        formatter: formatImage,
        sortable: false,
        search: false
      },
      {
        label: '추가2',
        name: 'GoodsImage2',
        width: 60,
        height: 60,
        align: 'center',
        formatter: formatImage,
        sortable: false,
        search: false
      },
      {
        label: '추가3',
        name: 'GoodsImage3',
        width: 60,
        height: 60,
        align: 'center',
        formatter: formatImage,
        sortable: false,
        search: false
      },
      {
        label: '추가4',
        name: 'GoodsImage4',
        width: 60,
        height: 60,
        align: 'center',
        formatter: formatImage,
        sortable: false,
        search: false
      },
    ],
    //page:1,
    //loadonce:true,
    //caption: '<h3><i class="fa fa-th-list"></i> 전체상품</h3>',
    rowNum: 50,
    rownumbers: true,
    //subGrid: true,
    //rownumWidth: 40,
    //gridview: true,
    autowidth: true,
    shrinkToFit: false, // 필드 width 를 responsive width 에 맞춘다.
    viewrecords: true,
    sortname: 'created',
    sortorder: "DESC",
    //scroll: 1, // set the scroll property to 1 to enable paging with scrollbar - virtual loading of records
    //scrollrows: true,
    //hoverrows: true,
    gridview: true, //처리속도를 빠르게 해준다. 시간측정시 절반가량 로딩시간 감소!!! 하지만 다음 모듈엔 사용할 수 없다!! ==> treeGrid, subGrid, afterInsertRow(event)
    multiselect: true,
    //onSelectRow: editRow,
    emptyrecords: '데이타가 없습니다.', // the message will be displayed at the bottom
    pager: "#jqGridPager",
    subGrid: false,
    subGridRowExpanded: showChildGrid,
    cellEdit: true,
    cellsubmit: 'remote',
    cellurl: '/products/index_edit',

    /*
    onSelectRow: function (rowId) {
    	//$("#jqGrid").jqGrid('editRow', rowId);
    	editRow(rowId);
    },
    jqGridAfterInsertRow: function(rowid, rowdata, rowelem)
    {
    	console.log(rowdata);
    	if(rowdata.activated == 1)
    	{
    		console.log(rowdata.activated);
    		$("#"+rowid).css("background", "#000000");
    	}
    }
    beforeSelectRow: function(rowid, e) {
    	console.log(rowid);
    	return false;
    }
    onCellSelect: function(rowid, iCol, cellcontent, e)
    {
    	//console.log(iCol);
    	// 기능 버튼 클릭 시 row 선택 안되게 하기
    	if(iCol == 2) $('#'+rowid).setSelection(rowid, false);
    }
    onSelectCell: function(rowid, cellname, value, iRow, iCol)
    {
    	console.log(cellname);
    },
    onCellSelect: function(rowid, iCol, cellcontent, e)
    {
    	console.log(e.target.childNodes[0].data);

    	// 기능 버튼 클릭 시 row 선택 안되게 하기
    	if(iCol == 13)
    	{
    		$(this).setSelection(rowid, false);
    		editRow(rowid);
    	}
    },
    */
    beforeSubmitCell: function(rowid, cellname, value, iRow, iCol) {
      console.log(rowid);
      var rowData = $("#jqGrid").getRowData(rowid);

      // 데이터 추가
      return {
        GoodsId: rowData.GoodsId
      };
    },
    loadBeforeSend: function() {
      LoadingModal.modal('show');
      $(this).closest("div.ui-jqgrid-view").find("table.ui-jqgrid-htable>thead>tr>th").css("text-align",
        "center");
    },
    loadComplete: function(data) {
        // 브라우저 jqgrid css table td 트러짐 방지 처리 다시 반영(2021.09.14)
        // 스크롤 페이징에 이것을 반영할려면 가로길이가 모니터 화면 가로길이보다 길어질때나 적용해야 반응형 화면에 이상이 없다.
        $('.ui-jqgrid-htable').attr('style', '');

        console.log(data.records);
        var tt = parseInt(goods_cnt) - parseInt(data.records);
        $("#GoodsUseTotalCnt").text(tt);
        if (tt <= 0) $("#GoodsCntTxt").text("촬영가능수량을 초과하셨습니다. 추가결재가 필요합니다.");
        LoadingModal.modal('hide');
    }
  });

  var lastSelection;

  function editRow(id) {
    console.log(id);
    //return;

    if (id && id !== lastSelection) {
      var grid = $("#jqGrid");
      var rowKey = grid.getGridParam('selarrrow');
      var rowData = grid.getRowData(rowKey);
      console.log(rowKey);
      console.log(rowData);

      if (lastSelection) {
        var rowid = lastSelection;
        //console.log('rowid : '+rowid);
        //grid.getRowData(lastSelection);
        var celldata = grid.getCell(lastSelection, 'GoodsEtc42');
        //console.log('celldata : '+celldata);

        grid.restoreRow(lastSelection);
      }

      var editParameters = {
        keys: true,
        extraparam: {
          GoodsId: rowData.GoodsId
        },
        successfunc: editSuccessful,
        errorfunc: editFailed,
        restoreAfterError: false
      };

      grid.jqGrid('editRow', id, editParameters);
      lastSelection = id;
    }
  }

  function editSuccessful(data, stat) {
    var response = $.parseJSON(data.responseText);
    //console.log(response);

    if (response.hasOwnProperty("error")) {
      //console.log(response.error);
      if (response.error.length) {
        return [false, response.error];
      }
    }
    return [true, "", ""];
  }

  function editFailed(rowID, response) {
    var response = $.parseJSON(response.responseText);
    //console.log(response);
    alert(response.error);
  }

  //$("#jqGrid").jqGrid('hideCol', ["GdsMstId"]);


  //$("#jqGrid").jqGrid("setLabel", "GoodsBtn", "", {"text-align":"center"});
  //$("#jqGrid").jqGrid("setLabel", "GoodsImage", "", {"text-align":"center"});
  //$("#jqGrid").jqGrid("setLabel", "market", "", {"text-align":"center"});

  /*
  $("#jqGrid").bind("jqGridSelectRow", function (e, rowid, orgClickEvent)
  {
  	console.log(rowid);
  });
  $("#jqGrid").bind("jqGridAfterLoadComplete", function (e, rowid, orgClickEvent)
  {
  	//console.log(rowid.rows);
  	for (var i = 0; i < rowid.rows.length; i++)
  	{
  		//console.log(rowid.rows[i].cell[7]);
  		var goodsno = rowid.rows[i].cell[2];
  		if(goodsno != '0')
  		{
  			//console.log(rowid.rows[i].id);
  			//$("#"+rowid.rows[i].id).addClass('warning');
  			//$("#"+rowid.rows[i].id).css("background", "#000000");
  			$(this).jqGrid('setRowData', rowid.rows[i].id, '', {background:'red'});
  		}
  	}
  	//$("#"+rowid).css("background", "#000000");
  });
  $("#jqGrid").navGrid("#jqGridPager",
  	{ edit: true, add: false, del: false, search: false, refresh: true, view: false, align: "left" },
  	{ closeAfterEdit: true, focusField : 1 }
  );
  */
  //jQuery("#jqGrid").jqGrid('navGrid','#jqGridPager',{del:false,add:false,edit:true},{closeAfterEdit: true, focusField : 1},{},{},{multipleSearch:true});

  //var sgrid = $("#jqGrid")[0];
  //sgrid.triggerToolbar();
  /*
  // activate the toolbar searching
  $('#jqGrid').jqGrid('filterToolbar', {
  	// JSON stringify all data from search, including search toolbar operators
  	//stringResult: true,
  	// instuct the grid toolbar to show the search options
  	searchOperators: true,
  	searchOnEnter: false,
  	ignoreCase: true
  });
  */
  //$('#jqGrid').jqGrid('filterToolbar');

  $("#jqGrid").jqGrid('navGrid', '#jqGridPager', {
    edit: false,
    add: false,
    del: false,
    search: false
  });
  $("#jqGrid").jqGrid('filterToolbar', {
    stringResult: true,
    searchOnEnter: true,
    defaultSearch: 'cn',
    ignoreCase: true
  });

  /*
  $('#jqGrid').navGrid('#jqGridPager',
  	// the buttons to appear on the toolbar of the grid
  	{ edit: false, add: false, del: false, search: false, refresh: true, view: false, position: "left", cloneToTop: false },
  	// options for the Edit Dialog
  	{
  		editCaption: "The Edit Dialog",
  		recreateForm: true,
  		checkOnUpdate : true,
  		checkOnSubmit : true,
  		closeAfterEdit: true,
  		errorTextFormat: function (data) {
  			return 'Error: ' + data.responseText
  		}
  	},
  	// options for the Add Dialog
  	{
  		closeAfterAdd: true,
  		recreateForm: true,
  		errorTextFormat: function (data) {
  			return 'Error: ' + data.responseText
  		}
  	},
  	// options for the Delete Dailog
  	{
  		errorTextFormat: function (data) {
  			return 'Error: ' + data.responseText
  		}
  	});
  */

  function fixSearchOperators() {
    var $grid = $("#jqGrid"),
      columns = $grid.jqGrid('getGridParam', 'colModel'),
      filterToolbar = $($grid[0].grid.hDiv).find("tr.ui-search-toolbar");

    filterToolbar.find("th").each(function(index) {
      var $searchOper = $(this).find(".ui-search-oper");
      if (!(columns[index].searchoptions && columns[index].searchoptions.searchOperators)) {
        $searchOper.hide();
      }
    });
  }

  function formatImage(cellValue, options, rowObject) {
    var imageHtml;
    //console.log(options);
    //console.log(typeof rowObject);
    //console.log(rowObject);

    var goods_no = rowObject[0];
    var userid = rowObject[20];
    //console.log(options);
    //console.log(typeof rowObject);
    // console.log('cellValue :>> ', cellValue);

    imageHtml = '<a target="_blank" href="http://'+userid+'.newtalk.kr/goods/detail/'+goods_no+'"><img width="58px" height="58px" /></a>';

    //var res = cellValue.split(".");
    //var imageHtml = "<img src='" + res[0] + "_thumb." + res[1] + "' width='60px' height='60px' originalValue='" + cellValue + "' />";
    if (cellValue != "") {
      imageHtml = '<a target="_blank" href="http://'+userid+'.newtalk.kr/goods/detail/'+goods_no+'"><img src="'+cellValue+'" width="58px" height="58px" originalValue="'+cellValue+'" /></a>';
    }
    return imageHtml;
  }

  function ButtonValue(cellvalue, options, rowObject) {
    //console.log(rowObject);
    var link;
    var goods_no = rowObject[2];

    // <button class="btn btn-success btn-xs btn-block" type="button" onclick="goodsProcess(\'' + rowObject[0] + '\', \'C\');">복사</button>
    if (goods_no != '0')
      link = '<button class="btn btn-primary btn-xs btn-block" type="button" onclick="goodsProcess(\'' + rowObject[
        0] +
      '\', \'E\');">수정</button><button class="btn btn-danger btn-xs btn-block" type="button" onclick="goodsProcess(\'' +
      rowObject[0] + '\', \'D\');">삭제</button>';
    else
      link = '<button class="btn btn-danger btn-xs btn-block" type="button" onclick="goodsProcess(\'' + rowObject[
        0] + '\', \'D\');">삭제</button>';

    return link;
  }

  function formatCreated(cellvalue, options, rowObject) {
    // console.log(cellvalue);
    var link;
    var goods_no = rowObject[0];

    link = '<p>' + cellvalue + '</p>';
    link += '<button class="btn btn-info btn-xs" type="button" onclick="goodsCreatedSet(\'' + goods_no + '\');">재등록일업데이트</button>';

    return link;
  }

  function SnsPostValue(cellvalue, options, rowObject) {
    //console.log(rowObject);
    var link;
    var goods_no = rowObject[0];

    link = '<button class="btn btn-warning btn-xs btn-block" type="button" onclick="snsProcess(\'' + rowObject[0] +
      '\', \'kakaostory\');">카카오스토리</button>';

    return link;
  }

  function GoodsCodeValue(cellvalue, options, rowObject) {
    //console.log(rowObject);
    var link;
    var goods_no = rowObject[0];
    var goods_code = rowObject[6];

    link = '';

    // <button class="btn btn-success btn-xs btn-block" type="button" onclick="goodsProcess(\'' + rowObject[0] + '\', \'C\');">복사</button>
    if (goods_code != '') {
      <?php if($down_level == 1) { ?>
      link += '<button class="btn btn-danger btn-xs" type="button" onclick="goodsCodeProcess(\'' + goods_no +
        '\', \'' + goods_code + '\', \'D\');">이미지<br>DOWN</button>';
      <?php } else { ?>
      link += '<button class="btn btn-danger btn-xs" type="button" onclick="alert(\'다운로드 권한이 없습니다.\');">이미지<br>DOWN</button>';
      <?php } ?>
    } else
      link += '';

    // 모바일 웹 브라우저 접속 시 다운로드 막기 (2022.01.11)
    let userAgent = navigator.userAgent;
    if(/Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(userAgent)) {
      if(userAgent.indexOf("newtalkapp") == -1) {   // 앱 접속이 아닐 때
      	//2024.11.7. 오병용대표님 요청으로 다운로드 버튼 활성화
        //link = '';
      }
    }

    return link;
  }

  function formatGoodsName(cellValue, options, rowObject) {
    var goods_no = rowObject[0];
    var goodsName = rowObject[5];
    var userid = rowObject[20];
    var goodsNameText;
    //console.log(options);
    //console.log(typeof rowObject);
    // console.log(rowObject);

    goodsNameText = '<a target="_blank" href="http://'+userid+'.newtalk.kr/goods/detail/'+goods_no+'">'+goodsName+'</a>';

    return goodsNameText;
  }

  function showChildGrid(parentRowID, parentRowKey) {
    var childGridID = parentRowID + "_table";
    var childGridPagerID = parentRowID + "_pager";

    var rowData = $('#jqGrid').getRowData(parentRowKey);

    // send the parent row primary key to the server so that we know which grid to show
    //var childGridURL = parentRowKey+".json";
    //childGridURL = childGridURL + "&parentRowID=" + encodeURIComponent(parentRowKey)

    // add a table and pager HTML elements to the parent grid row - we will render the child grid here
    $('#' + parentRowID).append('<table id=' + childGridID + '></table><div id=' + childGridPagerID +
      ' class=scroll></div>');

    $("#" + childGridID).jqGrid({
      url: "/products/goods_ajax_list/" + rowData.GdsMstId,
      mtype: "POST",
      datatype: "json",
      page: 1,
      colModel: [{
          label: '일련번호',
          name: 'GoodsId',
          key: true,
          hidden: true,
          editable: false
        },
        {
          label: '기능',
          name: 'GoodsBtn',
          key: false,
          width: 55,
          align: 'center',
          editable: false,
          sortable: false,
          formatter: ButtonValue
        },
        {
          label: '상품번호',
          name: 'GoodsNo',
          key: false,
          width: 100,
          align: 'center'
        },
        {
          label: '이미지',
          name: 'GoodsImage',
          width: 80,
          height: 80,
          align: 'center',
          formatter: formatImage,
          sortable: false
        },
        {
          label: '마켓구분',
          name: 'market',
          width: 100,
          align: 'center'
        },
        {
          label: '상품명',
          name: 'GoodsName',
          editable: true,
          width: 300
        }, // '100%'
        {
          label: '판매가격',
          name: 'GoodsPrice',
          editable: true,
          width: 100,
          align: 'center',
          formatter: 'integer',
          formatoptions: {
            defaultValue: 'No Value Set',
            thousandsSeparator: ','
          }
        },
        {
          label: '수량',
          name: 'GoodsCount',
          editable: true,
          width: 70,
          align: 'center',
          formatter: 'integer',
          formatoptions: {
            defaultValue: 'No Value Set',
            thousandsSeparator: ','
          }
        },
        {
          label: '최초등록일',
          name: 'created',
          width: 150,
          align: 'center'
        }
      ],
      //loadonce: true,
      autowidth: true,
      shrinkToFit: true, // 필드 width 를 responsive width 에 맞춘다.
      //viewrecords: true,
      scroll: 0,
      height: 'auto',
      pager: "#" + childGridPagerID,
      loadBeforeSend: function() {
        $(this).closest("div.ui-jqgrid-view").find("table.ui-jqgrid-htable>thead>tr>th").css("text-align",
          "center");
      },
      loadComplete: function() {
        $("#" + childGridPagerID).hide();
      }
    });

  }

  //$("#jqGrid").jqGrid('setGridWidth', $(".table-responsive").width(), true);

  $('#page-wrapper div.btn-group li').on('click', function(e) {
    var gb = $(this).attr('gb');

    switch (gb) {
      //case '1': getSelectedRow();	break;
      case '2':
        getMallSelectedView('Y');
        break;
      case '3':
        getMallSelectedView('N');
        break;
      case '4':
        getBestSelectedPlusMinus('P');
        break;
      case '5':
        getBestSelectedPlusMinus('M');
        break;
      case '6':
        getSelectedGoodsCodeProcess();
        break;
    }
  });
});

// 도매몰 상품이미지 썸네일 수정 모달
function showMallGoodsThumbnailModal() {
  //alert('this');
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  // console.log(rowKey);

  if (rowKey.length === 1) {
    var rowData;
    var goodsId = 0;

    rowData = grid.getRowData(rowKey);
    goodsId = rowData.GoodsId;

    // console.log(rowData);

    LoadingModal.modal('show');

    $.ajax({
      url: '/products/get_goods_thumbnail/',
      type: 'POST',
      data: {
        goodsId: goodsId
      },
      success: function(data) {
        LoadingModal.modal('hide');

        // console.log(data);
        try {
          var rtn = JSON.parse(data);
          // console.log(rtn);

          if (rtn.info['success'] == true)
          {
            $('#MallGoodsThumbnailModal h4.modal-title').html('상품 [ ' + rowData.GoodsName + ' ] 썸네일 수정');

            $('#MallGoodsThumbnailForm div.img-container img').attr('src', rtn.info['img']);
            $("#MallGoodsThumbnailForm span.width").text(rtn.info['width']);
            $("#MallGoodsThumbnailForm span.height").text(rtn.info['height']);
            // console.log(rtn.info['img']);
            $("#MallGoodsThumbnailForm input[name='goodsCode']").val(rowData.GoodsCode);

            MallGoodsThumbnailModal.modal('show');
            setTimeout(() => {
              cropperLoad(rtn.info['imgname'], rowData.GoodsCode);
            }, 300);
          } else {
            alert(rtn.info['text']);
            // window.location.reload();
          }
        } catch (err) {
          alert('처리 오류입니다.'); // err.message
          console.log(err);
        }
      },
      error: function(jqXHR, textStatus, errorThrown) {
        alert(errorThrown);
        LoadingModal.modal('hide');
      }
    });
  } else
    alert('선택된 상품이 없거나 상품 하나만 선택해야 합니다.');
}

function getSelectedDel() {
  //alert('this');
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  //console.log(rowKey);

  if (rowKey.length > 0) {
    var rowData;
    var goodsId = [];
    for (var i = 0; i < rowKey.length; i++) {
      rowData = grid.getRowData(rowKey[i]);
      goodsId[i] = rowData.GdsMstId;
    }

    if (confirm('삭제된 상품은 복구가 불가능합니다.\n\n삭제하겠습니까?')) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/erase_select/',
        type: 'POST',
        data: {
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);

            if (rtn.info['success'] == true) {
              alert(rtn.info['text']);
              grid.trigger("reloadGrid");
            } else
              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  } else
    alert('선택된 상품이 없습니다!');
}

// 베스트 상품 담기 / 해제
function getBestSelectedPlusMinus(gb) {
  //alert('준비중!');return;
  var confirmTxt;
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  //console.log(rowKey);

  if (gb == 'P') confirmTxt = '상품으로 담겠습니까?';
  else if (gb == 'M') confirmTxt = '상품에서 해제하겠습니까?';
  else {
    alert('잘못된 접근입니다.');
    return;
  }

  if (rowKey.length > 0) {
    var rowData;
    var goodsId = [];
    for (var i = 0; i < rowKey.length; i++) {
      rowData = grid.getRowData(rowKey[i]);
      goodsId[i] = rowData.GoodsId;
    }

    if (confirm('선택한 상품을 베스트 ' + confirmTxt)) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/goods_best_plus_minus_select/',
        type: 'POST',
        data: {
          gb: gb,
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);

            if (rtn.info['success'] == true) {
              alert(rtn.info['text']);
              grid.trigger("reloadGrid");
            } else
              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  } else
    alert('선택된 상품이 없습니다!');
}

// 상품 미니몰 노출 / 해제
function getMallSelectedView(gb) {
  //alert('준비중!');return;
  var confirmTxt;
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  //console.log(rowKey);

  if (gb == 'Y') confirmTxt = '상품으로 노출하습니까?';
  else if (gb == 'N') confirmTxt = '상품에서 해제하겠습니까?';
  else {
    alert('잘못된 접근입니다.');
    return;
  }

  if (rowKey.length > 0) {
    var rowData;
    var goodsId = [];
    for (var i = 0; i < rowKey.length; i++) {
      rowData = grid.getRowData(rowKey[i]);
      goodsId[i] = rowData.GoodsId;
    }

    if (confirm('선택한 상품을 미니몰 ' + confirmTxt)) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/goods_mall_view_select/',
        type: 'POST',
        data: {
          gb: gb,
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);

            if (rtn.info['success'] == true) {
              alert(rtn.info['text']);
              grid.trigger("reloadGrid");
            } else
              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  } else
    alert('선택된 상품이 없습니다!');
}

// 선택 상품 담기 해제
function getSelectedOut() {
  //alert('this');
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  //console.log(rowKey);

  if (rowKey.length > 0) {
    var rowData;
    var goodsId = [];
    for (var i = 0; i < rowKey.length; i++) {
      rowData = grid.getRowData(rowKey[i]);
      goodsId[i] = rowData.GdsMstId;
    }

    if (confirm('선택한 상품을 자동 전송 선택 상품에서 해제하겠습니까?')) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/minus_select/',
        type: 'POST',
        data: {
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);

            if (rtn.info['success'] == true) {
              alert(rtn.info['text']);
              grid.trigger("reloadGrid");
            } else
              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  } else
    alert('선택된 상품이 없습니다!');
}

function goodsProcess(goodsId, gb) {
  //console.log(goodsId);
  //alert(goodsId);

  //alert('준비중!!');return;

  if (!goodsId || !gb) {
    alert("정상적인 접근이 아닙니다!");
    return;
  }

  // 수정
  if (gb == "E") {
    window.location.href = '/products/editing/' + goodsId;
    //alert('준비중!!');return;
  }

  // 복사
  if (gb == "C") {
    //window.location.href = '/products/copying/'+goodsId;
    alert('준비중!!');
    return;
  }

  // 삭제
  if (gb == "D") {
    var grid = $("#jqGrid");

    if (confirm('삭제된 상품은 복구가 불가능합니다.\n\n삭제하겠습니까?')) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/erase/',
        type: 'POST',
        data: {
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);

            if (rtn.info['success'] == true) {
              //alert(rtn.info['text']);
              var rowid = grid.getGridParam('selrow');
              grid.trigger("reloadGrid");
              //grid.delRowData(rowid);
              //eliminarSeleccionados();

            } else
              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  }
}

// 상품코드
function goodsCodeProcess(goodsId, goodsCode, gb) {
	//console.log(goodsCode);
	//alert(goodsCode);
	
	//alert('준비중!!');return;
	
	if (!goodsId || !goodsCode || !gb) {
		alert("정상적인 접근이 아닙니다!");
		return;
	}
	
	// 관리
	if (gb == "E") {
		//window.location.href = '/products/goods_img/'+goodsCode;
		window.open('/products/goods_img/' + goodsCode);
		//alert('준비중!!');return;
	}
	
	// 다운
	if (gb == "D") {
		let userAgent = navigator.userAgent.toLowerCase();
		if (userAgent.match('newtalkapp')) {
			download_ajax(goodsId, goodsCode, 'app');
		}
		else if (window.ReactNativeWebView) {
			download_ajax(goodsId, goodsCode, 'rnapp');
		}
		else {
			// PC 웹: JSZip으로 CDN에서 직접 다운로드
			download_jszip(goodsId, goodsCode);
		}
	}
}

function download_jszip(goodsId, goodsCode) {
	if (typeof JSZip === 'undefined') {
		// JSZip 미로드 시 기존 방식 fallback
		window.open("/products/goods_code_zip_down?id=" + goodsId + "&code=" + goodsCode);
		return;
	}

	// 진행 표시
	var $btn = $('[onclick*="' + goodsCode + '"]').first();
	var origText = $btn.text();
	$btn.text('다운로드 중...').prop('disabled', true);

	$.getJSON('/products/goods_zip_urls?code=' + goodsCode, function(data) {
		if (!data.success) {
			alert(data.msg || '다운로드 오류입니다.');
			$btn.text(origText).prop('disabled', false);
			return;
		}

			var zip = new JSZip();
			var total = data.images.length;
			var loaded = 0;
			var failed = [];
			var concurrency = 5;
			var cursor = 0;
			var active = 0;

		// 상품 정보 텍스트 추가
		if (data.txt) {
			zip.file(data.txt_name, data.txt);
		}

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
				$.ajax({
					url: '/products/goods_zip_download_log',
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
					window.open("/products/goods_code_zip_down?id=" + goodsId + "&code=" + goodsCode);
					$btn.text(origText).prop('disabled', false);
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
					$btn.text(origText).prop('disabled', false);
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
								console.warn('이미지 다운 실패:', img.url, e);
								failed.push({
									url: img.url,
									zip_path: img.zip_path,
									error: e && e.message ? e.message : 'fetch failed'
								});
							})
							.then(function() {
								loaded++;
								active--;
								$btn.text('다운로드 중... (' + loaded + '/' + total + ')');
								runQueue();
							});
					})(data.images[cursor++]);
				}
			}

			if (!total) {
				alert('다운로드할 이미지가 없습니다.');
				$btn.text(origText).prop('disabled', false);
				return;
			}
			runQueue();
		}).fail(function() {
		// API 실패 시 기존 방식 fallback
		window.open("/products/goods_code_zip_down?id=" + goodsId + "&code=" + goodsCode);
		$btn.text(origText).prop('disabled', false);
	});
}

function download_ajax(id, code, gb) {
	var app_data = {}; // 픽업앱 전송 데이타(2021.11.09)
	
	if(!id && !code && !gb) {
		alert('정상적인 접근이 아닙니다.');
		return false;
	}
	
	$.ajax({
		url: '/products/goods_code_zip_down',
		type: 'GET',
		data: {
			gb: 'ajax',
			id: id,
			code: code
		},
		success: function(data) {
			// LoadingModal.modal('hide');
			try {
				var rtn = JSON.parse(data);
				if (rtn.info['success'] == true) {
					// console.log(rtn.info);
					
					let userAgent = navigator.userAgent.toLowerCase(); // 접속 핸드폰 정보
					if (userAgent.match('iphone')) {
						app_data.mobile = 'ios';
					}
					else if (userAgent.match('ipad')) {
						app_data.mobile = 'ios';
					}
					else if (userAgent.match('ipod')) {
						app_data.mobile = 'ios';
					}
					else if (userAgent.match('android')) {
						app_data.mobile = 'android';
					}
					else {
						app_data.mobile = 'other';
					}
					
					app_data.url = "https://newtalk.kr/data/files/pick/" + encodeURIComponent(rtn.info['downfile']);
					
					// console.log(JSON.stringify(app_data));
					
					if (gb == 'rnapp') {
						// react native app test
						window.ReactNativeWebView.postMessage(
							JSON.stringify({ app_data: app_data })
						);
					}
					else {
						if (userAgent.replace(/ /g,'').indexOf('iphoneos') > -1) {
							window.webkit.messageHandlers.download.postMessage(JSON.stringify(app_data));
						}
						else {
							//window.location.href="downimg://"+JSON.stringify(app_data);
							window.open(app_data.url);
						}
					}
					
					return;
				}
				else alert(rtn.info['text']);
			}
			catch (err) {
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

// 선택 상품코드
function getSelectedGoodsCodeProcess() {
  //alert('준비중!');return;
  var confirmTxt;
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  console.log(rowKey);

  if (rowKey.length > 0) {
    var rowData;
    var goodsCode = [];
    for (var i = 0; i < rowKey.length; i++) {
      rowData = grid.getRowData(rowKey[i]);
      console.log(rowData);
      goodsCode[i] = rowData.GoodsCode;

      /*
      if(rowData.GoodsCode)
      	window.open('/products/goods_code_zip_down/'+rowData.GoodsCode);
      else
      	alert('해당 상품[ '+rowData.GoodsName+' ]은 다운로드를 할 수 없습니다.');
      */
    }

    if (confirm('선택한 상품을 다운로드 하겠습니까?')) {
      LoadingModal.modal('show');

      $.ajax({
        url: '/products/goods_code_select_zip_down/',
        type: 'POST',
        data: {
          goodsCode: goodsCode
        },
        success: function(data) {
          LoadingModal.modal('hide');

          try {
            var rtn = JSON.parse(data);
            console.log(rtn);

	            if (rtn.info['success'] == true) {
	              //alert(rtn.info['text']);
	              //grid.trigger("reloadGrid");
	              var fileParam = rtn.info['file'] ? '?file=' + encodeURIComponent(rtn.info['file']) : '';
	              window.open('/products/goods_select_zip_down/P' + fileParam);
	            } else
	              alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
  } else
    alert('선택된 상품이 없습니다!');
}

function getSelectedRow() {
  //alert('this');
  var grid = $("#jqGrid");
  var rowKey = grid.getGridParam('selarrrow');
  //console.log(rowKey[0]);

  if (rowKey.length > 1) {
    alert('마켓개별등록은 한 개 상품만 가능합니다.');
    return;
  }

  if (rowKey[0]) {
    var rowData = grid.getRowData(rowKey[0]);
    var res = rowData.created.split(" ");
    var fullDate = new Date();
    //console.log(String(fullDate.getMonth()+1));
    var twoDigitMonth = ((String(fullDate.getMonth() + 1).length) === 2) ? (fullDate.getMonth() + 1) : '0' + (fullDate
      .getMonth() + 1);
    var twoDigitDay = ((String(fullDate.getDate()).length) === 2) ? (fullDate.getDate()) : '0' + (fullDate.getDate());
    var currentDate = fullDate.getFullYear() + "-" + twoDigitMonth + "-" + twoDigitDay;
    //console.log(fullDate.getTime());

    console.log(res[0]);
    console.log(currentDate);

    if (res[0] != currentDate) {
      //alert("오늘 등록된 상품만 개별 등록이 가능합니다.");
      //return;
    }

    //console.log(rowData);
    LoadingModal.modal('show');

    $.ajax({
      url: '/products/master_market_select_update/',
      type: 'POST',
      data: {
        mtid: rowData.GdsMstId
      },
      success: function(data) {
        LoadingModal.modal('hide');

        try {
          var rtn = JSON.parse(data);
          //console.log(rtn);

          if (rtn.info['success'] == true || rtn.info['success'] == false) {
            alert(rtn.info['text']);
            if (rtn.info['success'] == true) grid.trigger("reloadGrid");
          } else
            alert('처리 오류(1)입니다.');
        } catch (err) {
          alert('처리 오류(2)입니다.'); // err.message
        }
      },
      error: function(jqXHR, textStatus, errorThrown) {
        alert(errorThrown);
        LoadingModal.modal('hide');
      }
    });
  } else
    alert("선택된 상품이 없습니다!");
}

// 해당 상품 재등록일 처리
function goodsCreatedSet(goodsId)
{
    //console.log(goodsId);
    //alert(goodsId);
    //alert('준비중!!');return;
    if (!goodsId) {
        alert("정상적인 접근이 아닙니다!");
        return;
    }

    var grid = $("#jqGrid");
    if (confirm('해당 상품 재등록일을 현재 시간으로 업데이트 합니다.\n\n처리하겠습니까?')) {
      LoadingModal.modal('show');
      $.ajax({
        url: '/products/goods_created_set/',
        type: 'POST',
        data: {
          goodsId: goodsId
        },
        success: function(data) {
          LoadingModal.modal('hide');
          try {
            var rtn = JSON.parse(data);
            //console.log(rtn);
            if (rtn.info['success'] == true) {
              //alert(rtn.info['text']);
              var rowid = grid.getGridParam('selrow');
              grid.trigger("reloadGrid");
              //grid.delRowData(rowid);
              //eliminarSeleccionados();
            } else alert(rtn.info['text']);
          } catch (err) {
            alert('처리 오류입니다.'); // err.message
            console.log(err);
          }
        },
        error: function(jqXHR, textStatus, errorThrown) {
          alert(errorThrown);
          LoadingModal.modal('hide');
        }
      });
    }
}

function snsProcess(goodsId, gb) {
  //console.log(goodsId);
  //alert(gb);

  //alert('준비중!!');return;

  if (!goodsId || !gb) {
    alert("정상적인 접근이 아닙니다!");
    return;
  }

  if (gb == 'kakaostory')
    loginWithKakao(goodsId);
  else
    alert("정상적인 접근이 아닙니다!");
}

// 등록 처리중 모달창 처리
var opts = {
  lines: 13, // The number of lines to draw
  length: 20, // The length of each line
  width: 10, // The line thickness
  radius: 30, // The radius of the inner circle
  corners: 1, // Corner roundness (0..1)
  rotate: 0, // The rotation offset
  direction: 1, // 1: clockwise, -1: counterclockwise
  color: '#000', // #rgb or #rrggbb or array of colors
  speed: 1, // Rounds per second
  trail: 60, // Afterglow percentage
  shadow: false, // Whether to render a shadow
  hwaccel: false, // Whether to use hardware acceleration
  className: 'spinner', // The CSS class to assign to the spinner
  zIndex: 2e9, // The z-index (defaults to 2000000000)
  top: 'auto', // Top position relative to parent in px
  left: 'auto' // Left position relative to parent in px
};
var target = document.getElementById('loading_spinner_center');
var spinner = new Spinner(opts).spin(target);

// 썸네일 이미지 처리
function cropperLoad(downloadedImageName, goodsCode)
{
  var Cropper = window.Cropper;
  var URL = window.URL || window.webkitURL;
  var container = document.querySelector('.img-container');
  var image = container.getElementsByTagName('img').item(0);
  var download = document.getElementById('download');
  var actions = document.getElementById('actions');
  var dataHeight = document.getElementById('dataHeight');
  var dataWidth = document.getElementById('dataWidth');
  var options = {
    aspectRatio: 'free',
    preview: false,
    autoCrop: false,
    ready: function (e) {
      console.log(e.type);
    },
    cropstart: function (e) {
      console.log(e.type, e.detail.action);
    },
    cropmove: function (e) {
      console.log(e.type, e.detail.action);
    },
    cropend: function (e) {
      console.log(e.type, e.detail.action);
    },
    crop: function (e) {
      var data = e.detail;

      console.log(e.type);
      dataHeight.value = Math.round(data.height);
      dataWidth.value = Math.round(data.width);
    },
    zoom: function (e) {
      console.log(e.type, e.detail.ratio);
    }
  };
  var cropper = new Cropper(image, options);
  var originalImageURL = image.src;
  var uploadedImageType = 'image/jpeg';
  var uploadedImageName = 'cropped.jpg';
  var uploadedImageURL;

  if(downloadedImageName) uploadedImageName = downloadedImageName;
  // console.log(uploadedImageName);

  // Tooltip
  $('[data-toggle="tooltip"]').tooltip();

  // Buttons
  if (!document.createElement('canvas').getContext) {
    $('button[data-method="getCroppedCanvas"]').prop('disabled', true);
  }

  if (typeof document.createElement('cropper').style.transition === 'undefined') {
    $('button[data-method="rotate"]').prop('disabled', true);
    $('button[data-method="scale"]').prop('disabled', true);
  }

  // Download
  if (typeof download.download === 'undefined') {
    download.className += ' disabled';
    download.title = 'Your browser does not support download';
  }

  // Methods
  actions.querySelector('.docs-buttons').onclick = function (event) {
    var e = event || window.event;
    var target = e.target || e.srcElement;
    var cropped;
    var result;
    var input;
    var data;

    if (!cropper) {
      return;
    }

    while (target !== this) {
      if (target.getAttribute('data-method')) {
        break;
      }

      target = target.parentNode;
    }

    if (target === this || target.disabled || target.className.indexOf('disabled') > -1) {
      return;
    }

    data = {
      method: target.getAttribute('data-method'),
      target: target.getAttribute('data-target'),
      option: target.getAttribute('data-option') || undefined,
      secondOption: target.getAttribute('data-second-option') || undefined
    };

    cropped = cropper.cropped;

    if (data.method) {
      if (typeof data.target !== 'undefined') {
        input = document.querySelector(data.target);

        if (!target.hasAttribute('data-option') && data.target && input) {
          try {
            data.option = JSON.parse(input.value);
          } catch (e) {
            console.log(e.message);
          }
        }
      }

      switch (data.method) {
        case 'save':
          cropper.getCroppedCanvas().toBlob((blob) =>
          {
            if(!goodsCode) {
              alert('상품 정보가 없습니다.');
              return;
            }
            const formData = new FormData();

            // Pass the image file name as the third parameter if necessary.
            formData.append('croppedImage', blob);
            formData.append('goodsCode', goodsCode);

            LoadingModal.modal('show');

            // console.log(formData);
            // Use `jQuery.ajax` method for example
            $.ajax('/products/set_goods_thumbnail/', {
              method: 'POST',
              data: formData,
              processData: false,
              contentType: false,
              success(data) {
                console.log(data);
                var rtn = JSON.parse(data);
                //console.log(rtn);
                if (rtn.info['success'] == true) {
                  MallGoodsThumbnailModal.modal('hide');
                } else alert(rtn.info['text']);
                LoadingModal.modal('hide');
              },
              error() {
                LoadingModal.modal('hide');
                console.log('Upload error');
              },
            });
          }, 'image/jpeg', 0.9);
          return;
          break;

        case 'getCroppedCanvas':
          try {
            data.option = JSON.parse(data.option);
          } catch (e) {
            console.log(e.message);
          }

          if (uploadedImageType === 'image/jpeg') {
            if (!data.option) {
              data.option = {};
            }

            data.option.fillColor = '#fff';
          }

          break;
      }

      result = cropper[data.method](data.option, data.secondOption);
      console.log(result);

      switch (data.method) {
        case 'getCroppedCanvas':
          if (result) {
            // Bootstrap's Modal
            $('#getCroppedCanvasModal').modal().find('.modal-body').html(result);

            if (!download.disabled) {
              download.download = uploadedImageName;
              download.href = result.toDataURL(uploadedImageType);
            }
          }

          break;
      }

      if (typeof result === 'object' && result !== cropper && input) {
        try {
          input.value = JSON.stringify(result);
        } catch (e) {
          console.log(e.message);
        }
      }
    }
  };

  document.body.onkeydown = function (event) {
    var e = event || window.event;

    if (e.target !== this || !cropper || this.scrollTop > 300) {
      return;
    }

    switch (e.keyCode) {
      case 37:
        e.preventDefault();
        cropper.move(-1, 0);
        break;

      case 38:
        e.preventDefault();
        cropper.move(0, -1);
        break;

      case 39:
        e.preventDefault();
        cropper.move(1, 0);
        break;

      case 40:
        e.preventDefault();
        cropper.move(0, 1);
        break;
    }
  };

  // Import image
  var inputImage = document.getElementById('inputImage');

  if (URL) {
    inputImage.onchange = function () {
      var files = this.files;
      var file;

      if (files && files.length) {
        file = files[0];

        if (/^image\/\w+/.test(file.type)) {
          uploadedImageType = file.type;
          // uploadedImageName = file.name;

          if (uploadedImageURL) {
            URL.revokeObjectURL(uploadedImageURL);
          }

          image.src = uploadedImageURL = URL.createObjectURL(file);
          // console.log(cropper.getImageData());

          if (cropper) {
            cropper.destroy();
          }

          cropper = new Cropper(image, options);
          inputImage.value = null;

          image.onload = function () {
            // console.log(image.naturalWidth);
            // console.log(image.naturalHeight);
            $("#MallGoodsThumbnailForm span.width").text(image.naturalWidth);
            $("#MallGoodsThumbnailForm span.height").text(image.naturalHeight);
          };
          // console.log(image.naturalWidth);
          // console.log(cropper.cropper("getImageData"));
          // console.log(cropper.viewBoxImage.naturalHeight);
        } else {
          window.alert('Please choose an image file.');
        }
      }
    };
  } else {
    inputImage.disabled = true;
    inputImage.parentNode.className += ' disabled';
  }
};
</script>
